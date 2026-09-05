# OWLERY Cache Reload

This repository contains a script to cache OWLERY queries for Virtual Fly Brain (VFB) by running all possible queries with all potential anatomy IDs.

## Purpose

After each release of VFB, the OWLERY query server needs to have its cache populated with results for all possible queries to ensure fast response times for user queries.

The script extracts OWLERY queries from the [queries_execution_notebook.ipynb](https://github.com/VirtualFlyBrain/geppetto-vfb/blob/master/model/queries_execution_notebook.ipynb), determines the restrictions on potential IDs (anatomy classes), uses VFBconnect to pull all potential anatomy IDs from the PDB database, and then runs each query against the OWLERY server to cache the results.

It also pre-warms the `v3-cached` (VFBquery) layer. The set of `run_query` query types it covers is kept in step with the query types the v2 Geppetto frontend can fire, defined as `CompoundRefQuery` entries in [geppetto-vfb/model/vfb.xmi](https://github.com/VirtualFlyBrain/geppetto-vfb/blob/master/model/vfb.xmi). Each query's `id_filter` mirrors that query's `matchingCriteria`, so every term the v2 UI can offer a query for is warmed and end users never hit a cold start after a release. The only frontend query type deliberately not pre-warmed is `SimilarMorphologyToUserData`, which operates on user-uploaded data and has nothing to cache. If a new query type is added to the xmi, add a matching entry here.

## How it runs

The script `main.py`:
1. Connects to the VFB database using VFBconnect.
2. Retrieves all anatomy class short_form IDs using a Cypher query.
3. Sorts the IDs in descending order to process the newest ones first.
4. For each predefined OWLERY query and each anatomy ID, constructs the query URL and sends a GET request to the OWLERY server.
4. Paces those requests against the live service (see [Background pacing](#background-pacing) below).
5. Logs a success indicator (✓) with result count for successful queries, or error details with URL for failures.

Run with:
```
source .venv/bin/activate
python main.py [--max-ids N] [--timeout T] [--parallel P] [--force-refresh] [--only TOKENS] [--skip TOKENS] [--list-servers]
```

| Flag | Effect |
| --- | --- |
| `--max-ids N` | Limit to the first N IDs per query (for testing). |
| `--timeout T` | Per-request timeout in seconds (default 200, just past the server's 180s response budget). |
| `--parallel P` | Ceiling on requests in flight across the **whole sweep** (default 4). Global, not per query type. |
| `--start-parallel N` | Requests in flight before the governor has seen any status readings (default 1). |
| `--threads-per-query N` | Worker threads per query type (default 4). Bounds how many requests may be queued ready to go; `--parallel` decides how many are actually in flight. |
| `--status-url URL` | VFBquery status endpoint used to pace the sweep (default `https://vfbquery.virtualflybrain.org/status`). Pass an empty string to disable pacing and run at a fixed `--parallel`. |
| `--poll-interval S` | Seconds between status polls (default 10). |
| `--idle-fraction F` | Fraction of the service's `max_concurrent` below which it counts as idle enough to speed up (default 0.25). |
| `--pause-seconds S` | How long to stand down completely after finding the service saturated (default 60). |
| `--retries N` | Retries for busy responses (429/502/503/504) before giving up on an ID (default 4). |
| `--backoff-base S` | First retry delay in seconds; doubles per attempt with jitter (default 15). |
| `--backoff-cap S` | Maximum retry delay in seconds (default 300). |
| `--force-refresh` | Send `X-Force-Refresh: true` on every request so the v3-cached layer bypasses its cache and overwrites the canonical slot with the fresh upstream response. Use after a VFBquery release to pre-warm the cache. |
| `--only TOKENS` | Run only query types whose backend server host **or** name contains one of the comma-separated tokens (case-insensitive substring). Applied before `--skip`. |
| `--skip TOKENS` | Skip query types whose backend server host **or** name contains one of the tokens. Applied after `--only`. |
| `--list-servers` | Print the backend servers and the query types targeting each, then exit. Use to see what tokens `--only`/`--skip` will match. |

Some queries may time out, but the cache will still be populated for successful ones.

## Background pacing

This is a housekeeping job. It must never compete with live user traffic for VFBquery capacity, so it is built to trickle along in the background and get out of the way the moment anyone else needs the service.

Two mechanisms do that.

**One global cap, not one per query type.** Every query type shares a single `AdaptiveLimiter`, so the number of requests in flight is `--parallel` in total no matter how many query types are selected. Adding a query type no longer multiplies the load. (Before this, each of the 53 query types opened its own pool of `--parallel` threads and all pools ran at once, so the sweep could offer the service thousands of concurrent requests against a `max_concurrent` in the tens.)

**A governor that reads the service's own `/status`.** A background thread polls `--status-url` every `--poll-interval` seconds and moves the cap using additive-increase / multiplicative-decrease:

- `waiting > 0` — something is queued, so somebody is being made to wait. Halve the cap.
- `active >= max_concurrent` **and** `waiting > 0` — the service is saturated. Drop to zero and stand down for `--pause-seconds`.
- `active <= max_concurrent * --idle-fraction` and nothing queued — the service is genuinely idle. Add one, up to the `--parallel` ceiling.
- Status unreadable three polls running — hold at 1 rather than fly blind.

The cap starts at `--start-parallel` (1) and has to earn its way up, so a cold start never arrives as a burst. After a pause it resumes at 1, not at whatever it had reached before.

**Busy responses are retried, not discarded.** A 503 from VFBquery means *"still computing, the result will be cached, retry shortly"* — retrying is precisely what populates the cache. 429, 502, 503 and 504 are retried with exponential backoff and full jitter (`--backoff-base` doubling to `--backoff-cap`), honouring `Retry-After` when the service sends one. The backoff happens **outside** the limiter, so a waiting thread never occupies capacity it is not using. Any other non-200 is a real problem with that query and is reported immediately without retrying.

To run without pacing — for example against a private instance where the sweep is the only client:

```
python main.py --status-url "" --parallel 20
```

The pacing logic has offline unit tests that need no network and generate no traffic:

```
python3 -m unittest test_throttle -v
```

### Refreshing selected servers

Queries run against two backend hosts: `owl.virtualflybrain.org` (legacy OWLERY) and `v3-cached.virtualflybrain.org` (the V3 cache). `--only`/`--skip` match against the host, the query-type name, or an explicit per-query tag, so they can target a whole server, a single query type, or a tagged group without a separate group taxonomy.

Tags currently defined: `flybase`/`stocks` (find_stocks), `connectivity`, `dataset`, `expression`, `morphology`, `nblast`, `neuronbridge`, `pub`, `scrnaseq`. Run `--list-servers` to see the full, current list. So e.g. `--only scrnaseq` warms just the four single-cell RNAseq query types, `--only dataset` the dataset queries, and so on.

```
# See the servers and their query types
python main.py --list-servers

# Refresh only OWLERY
python main.py --only owl --force-refresh

# Refresh everything except OWLERY (i.e. only the V3 cache)
python main.py --skip owl --force-refresh

# Refresh the V3 cache only
python main.py --only v3-cached --force-refresh

# Refresh a single query type by name
python main.py --only NeuronInputsTo

# Refresh just the FlyBase stocks query via its tag
python main.py --only flybase --force-refresh
```

The script is designed to run in a Jenkins job with Python 3.10 after each VFB release.

### Common Jenkins `OPTIONALOPTS` recipes

The `PreLoad_OWL_server` Jenkins job passes its `OPTIONALOPTS` string parameter straight into the shell as `${OPTIONALOPTS}`, **unquoted**. Any value containing a space gets word-split by bash before `main.py` ever sees it -- a quoted multi-word value like `--skip "a b,c d"` turns into several stray positional arguments and `main.py` exits with an argparse error before doing anything (this is what happened in build #90; nothing had run yet at that point). Until that job is fixed (candidate fix: wrap the invocation in `eval` so quoting works, at the cost of turning `OPTIONALOPTS` into a shell-injection surface for anyone with build permission on the job -- deliberately left unresolved as a trade-off), **every value below is written with no spaces**, using commas to join `--only`/`--skip` tokens and single distinctive words instead of quoted phrases. Keep new recipes to this same style unless the job's quoting is fixed first.

| Goal | `OPTIONALOPTS` |
| --- | --- |
| Full sweep (default, after a release) | *(leave blank)* `--force-refresh` |
| `V3 term info` cache only | `--only info --force-refresh` |
| VFBquery/`v3-cached` layer only (skip raw Owlery) | `--only v3-cached --force-refresh` |
| Raw Owlery (legacy `owl.virtualflybrain.org`) only, all query types | `--only owl --force-refresh` |
| Raw Owlery **essentials only** -- just the two queries reused as building blocks by everything else (`Owlery SubclassesOf`, `Owlery Part of`; see `VFBquery/src/vfbquery/vfb_queries.py`'s `_get_all_children` helper, which calls exactly these two for every subclass/part-of hierarchy traversal) | `--only owl --skip here,presynaptic,postsynaptic,overlaps,synaptic,develops --force-refresh` |
| One tagged group (e.g. scRNAseq) | `--only scrnaseq --force-refresh` |

`--only`/`--skip` match on host, query-type name, or tag as a case-insensitive substring (see above), so a new recipe is usually one distinctive, space-free word away. Run `--list-servers` first to see every current query-type name and tag, pick a word unique to what you want to keep or drop, and check it against the full list before using it in Jenkins -- a substring that also matches something you didn't intend will silently pull it in too.

### Full token reference (every query type)

One space-free token per query type, chosen to collide with as few other query types as possible (never zero-cost: some genuinely share a word, noted in the last column). Generated by simulating `main.py`'s own `--only`/`--skip` matching logic against the live `queries` list, not hand-picked -- re-run that check after editing `main.py` before trusting this table again. Where a token "also matches" something, combine it with `--only owl` / `--only v3-cached` (or an extra `--skip` word from this same table) to isolate the one you want, the same way the "essentials" recipe above combines six skip words to isolate two queries.

#### Raw Owlery queries (`owl.virtualflybrain.org`)

| Query name | Token | What it does | Collision note |
| --- | --- | --- | --- |
| `Owlery Neuron class with part here` | `part` | Neuron classes with some part at the given anatomy term (raw Owlery). | also matches: Owlery Part of; Owlery Images of neurons with some part here; V3 PartsOf; V3 NeuronsPartHere; V3 SimilarMorphologyToPartOf; V3 SimilarMorphologyToPartOfexp |
| `Owlery Neurons Presynaptic` | `presynaptic` | Neuron classes with presynaptic terminals in the given anatomy term. | also matches: V3 NeuronsPresynapticHere |
| `Owlery Neurons Postsynaptic` | `postsynaptic` | Neuron classes with postsynaptic terminals in the given anatomy term. | also matches: V3 NeuronsPostsynapticHere |
| `Owlery Neuron classes fasciculating here` | `fasciculating` | Named "fasciculating" but actually precaches tracts/nerves innervating the term (RO_0002134/FBbt_00005099) -- a real query, just mislabeled; see PR #6. | also matches: V3 NeuronClassesFasciculatingHere |
| `Owlery Neurons Synaptic` | `synaptic` | Neuron classes with synaptic terminals (any) in the given anatomy term. Fixed in PR #6. | also matches: Owlery Neurons Presynaptic; Owlery Neurons Postsynaptic; V3 NeuronsSynaptic; V3 NeuronsPresynapticHere; V3 NeuronsPostsynapticHere |
| `Owlery SubclassesOf` | `subclasses` | Bare subclass closure, no anchor class -- the building block reused by VFBquery's hierarchy traversal for every subclass-mode query. | also matches: V3 SubclassesOf |
| `Owlery Part of` | `part` | Bare BFO_0000050 (part_of) closure, no anchor class -- the part_of-mode counterpart reused the same way. | also matches: Owlery Neuron class with part here; Owlery Images of neurons with some part here; V3 PartsOf; V3 NeuronsPartHere; V3 SimilarMorphologyToPartOf; V3 SimilarMorphologyToPartOfexp |
| `subClassOf cell that overlaps some X` | `cell` | Cell subclasses overlapping the given anatomy term. Fixed in PR #6 (was anchored on the wrong "cell", CL_0000000). | unique |
| `Owlery Images of neurons with some part here` | `part` | Individual neuron images with some part at the given anatomy term. | also matches: Owlery Neuron class with part here; Owlery Part of; V3 PartsOf; V3 NeuronsPartHere; V3 SimilarMorphologyToPartOf; V3 SimilarMorphologyToPartOfexp |
| `Images of neurons that develops from this` | `develops` | Individual neuron images that develop from the given anatomy term. Fixed in PR #6 (was part_of, not develops_from). | unique |

#### V3-cached queries (`v3-cached.virtualflybrain.org`, via VFBquery)

| Query name | Token | Tags | What it does | Collision note |
| --- | --- | --- | --- | --- |
| `V3 term info` | `info` |  | VFBquery's get_term_info cache -- the term page's main data blob. | unique |
| `V3 ListAllAvailableImages` | `available` |  | Every image available for a term. | unique |
| `V3 PartsOf` | `parts` |  | VFBquery's cached PartsOf query type. | unique |
| `V3 SubclassesOf` | `subclasses` |  | VFBquery's cached SubclassesOf query type (the v3-cached counterpart of the raw Owlery one above). | also matches: Owlery SubclassesOf |
| `V3 NeuronInputsTo` | `inputs` |  | Neurons this neuron provides input to. | unique |
| `V3 NeuronNeuronConnectivityQuery` | `neuronneuronconnectivityquery` |  | Neuron-to-neuron connectivity for an individual neuron. | unique |
| `V3 NeuronsPartHere` | `neuronsparthere` |  | Neurons with some part at this anatomy term (v3-cached). | unique |
| `V3 NeuronsSynaptic` | `neuronssynaptic` |  | Neurons with synaptic terminals here (v3-cached). | unique |
| `V3 PaintedDomains` | `domains` |  | Painted anatomical domains on a template. | unique |
| `V3 AllAlignedImages` | `allalignedimages` |  | All images aligned to a template. | unique |
| `V3 AllDatasets` | `alldatasets` |  | All datasets aligned to a template. | unique |
| `V3 SimilarMorphologyTo` | `similar` | morphology | Neurons with similar morphology (NBLAST) to an individual neuron. | also matches: V3 SimilarMorphologyToPartOf; V3 SimilarMorphologyToPartOfexp; V3 SimilarMorphologyToNB; V3 SimilarMorphologyToNBexp |
| `V3 TransgeneExpressionHere` | `transgene` | expression | Transgenes expressed at this anatomy term. | unique |
| `V3 AnatomyExpressedIn` | `anatomy` | expression | Anatomy terms a gene/transgene is expressed in. | unique |
| `V3 epFrag` | `frag` | expression | Expression pattern fragments for a term. | unique |
| `V3 NeuronClassesFasciculatingHere` | `neuronclassesfasciculatinghere` |  | The REAL fasciculating-here query (RO_0002101 on FBbt_00005106) -- not currently precached by any raw-Owlery entry; see PR #6 followups. | unique |
| `V3 ImagesNeurons` | `imagesneurons` |  | Images of neurons for a term. | unique |
| `V3 NeuronsPresynapticHere` | `neuronspresynaptichere` |  | Neurons with presynaptic terminals here (v3-cached). | unique |
| `V3 NeuronsPostsynapticHere` | `neuronspostsynaptichere` |  | Neurons with postsynaptic terminals here (v3-cached). | unique |
| `V3 TractsNervesInnervatingHere` | `nerves` |  | Tracts/nerves innervating this term -- what the mislabeled raw "fasciculating here" entry actually warms. | unique |
| `V3 ComponentsOf` | `components` |  | Components of a term. | unique |
| `V3 LineageClonesIn` | `clones` |  | Lineage clones found in this term -- no raw-Owlery precache entry exists for this at all. | unique |
| `V3 ImagesThatDevelopFrom` | `imagesthatdevelopfrom` |  | Images that develop from this term (v3-cached). | unique |
| `V3 UpstreamClassConnectivity` | `upstream` | connectivity | Class-level upstream connectivity. | unique |
| `V3 DownstreamClassConnectivity` | `downstream` | connectivity | Class-level downstream connectivity. | unique |
| `V3 NeuronRegionConnectivityQuery` | `region` | connectivity | Individual neuron-to-region connectivity. | unique |
| `V3 DatasetImages` | `datasetimages` | dataset | Images belonging to a dataset. | unique |
| `V3 AlignedDatasets` | `aligneddatasets` | dataset | Datasets aligned to a template. | unique |
| `V3 TermsForPub` | `terms` | pub | Terms associated with a publication. | unique |
| `V3 SimilarMorphologyToPartOf` | `similarmorphologytopartof` | morphology, nblast | NBLAST-similar neurons that are part of an expression pattern. | also matches: V3 SimilarMorphologyToPartOfexp |
| `V3 SimilarMorphologyToPartOfexp` | `ofexp` | morphology, nblast | NBLAST-similar expression patterns that are part of a neuron. | unique |
| `V3 SimilarMorphologyToNB` | `similarmorphologytonb` | morphology, neuronbridge | NeuronBridge-similar matches for an expression pattern. | also matches: V3 SimilarMorphologyToNBexp |
| `V3 SimilarMorphologyToNBexp` | `bexp` | morphology, neuronbridge | NeuronBridge-similar matches for a neuron. | unique |
| `V3 anatScRNAseqQuery` | `aseq` | scrnaseq | scRNAseq results for an anatomy class. | unique |
| `V3 clusterExpression` | `clusterexpression` | scrnaseq | Expression data for a scRNAseq cluster. | unique |
| `V3 scRNAdatasetData` | `adataset` | scrnaseq | scRNAseq dataset summary data. | unique |
| `V3 expressionCluster` | `expressioncluster` | scrnaseq | scRNAseq clusters expressing a gene. | unique |
| `V3 resolve_entity` | `entity` |  | Resolve a FlyBase entity ID. | unique |
| `V3 find_stocks` | `stocks` | flybase, stocks | Find stocks/strains for an FBgn/FBal/FBti/FBco/FBst ID. | unique |
| `V3 resolve_combination` | `combination` |  | Resolve a feature-combination (FBco) ID. | unique |
| `V3 find_combo_publications` | `combo` |  | Publications for a feature combination. | unique |
| `V3 list_connectome_datasets` | `connectome` |  | Global: list all connectome datasets (no ID). | unique |
| `V3 query_connectivity` | `query_connectivity` |  | Global: a fixed sample connectivity query (no ID). | unique |

## Dependencies

Create and activate a virtual environment:
```
python3 -m venv .venv
source .venv/bin/activate
```

Install with:
```
pip install -r requirements.txt
```

## Files

- `.venv/`: Python virtual environment.
- `.gitignore`: Git ignore file.
- `main.py`: The main script.
- `throttle.py`: Global rate limiter and the `/status`-driven governor.
- `test_throttle.py`: Offline unit tests for the pacing logic.
- `requirements.txt`: Python dependencies.
- `LICENSE`: MIT License.
- `README.md`: This documentation.
