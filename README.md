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
4. Runs queries concurrently (up to the specified number of parallel requests per ID) to speed up caching.
5. Logs a success indicator (✓) with result count for successful queries, or error details with URL for failures.

Run with:
```
source .venv/bin/activate
python main.py [--max-ids N] [--timeout T] [--parallel P] [--force-refresh] [--only TOKENS] [--skip TOKENS] [--list-servers]
```

| Flag | Effect |
| --- | --- |
| `--max-ids N` | Limit to the first N IDs per query (for testing). |
| `--timeout T` | Per-request timeout in seconds (default 9000). |
| `--parallel P` | Number of parallel requests per query type (default 50). |
| `--force-refresh` | Send `X-Force-Refresh: true` on every request so the v3-cached layer bypasses its cache and overwrites the canonical slot with the fresh upstream response. Use after a VFBquery release to pre-warm the cache. |
| `--only TOKENS` | Run only query types whose backend server host **or** name contains one of the comma-separated tokens (case-insensitive substring). Applied before `--skip`. |
| `--skip TOKENS` | Skip query types whose backend server host **or** name contains one of the tokens. Applied after `--only`. |
| `--list-servers` | Print the backend servers and the query types targeting each, then exit. Use to see what tokens `--only`/`--skip` will match. |

Some queries may time out, but the cache will still be populated for successful ones.

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
- `requirements.txt`: Python dependencies.
- `LICENSE`: MIT License.
- `README.md`: This documentation.
