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
