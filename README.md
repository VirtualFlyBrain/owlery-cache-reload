# OWLERY Cache Reload

This repository contains a script to cache OWLERY queries for Virtual Fly Brain (VFB) by running all possible queries with all potential anatomy IDs.

## Purpose

After each release of VFB, the OWLERY query server needs to have its cache populated with results for all possible queries to ensure fast response times for user queries.

The script extracts OWLERY queries from the [queries_execution_notebook.ipynb](https://github.com/VirtualFlyBrain/geppetto-vfb/blob/master/model/queries_execution_notebook.ipynb), determines the restrictions on potential IDs (anatomy classes), uses VFBconnect to pull all potential anatomy IDs from the PDB database, and then runs each query against the OWLERY server to cache the results.

## How it runs

The script `main.py`:
1. Connects to the VFB database using VFBconnect.
2. Retrieves all anatomy class short_form IDs using a Cypher query.
3. For each predefined OWLERY query and each anatomy ID, constructs the query URL and sends a GET request to the OWLERY server.
4. Runs queries concurrently (up to the specified number of parallel requests per ID) to speed up caching.
5. Logs a success indicator (✓) with result count for successful queries, or error details with URL for failures.

Run with:
```
source .venv/bin/activate
python main.py [--max-ids N] [--timeout T] [--parallel P]
```

Where `--max-ids N` limits to the first N IDs per query for testing (optional), `--timeout T` sets the timeout in seconds for each request (default 60), and `--parallel P` sets the number of parallel requests to run at once (default 9).

Each request has a configurable timeout (default 60 seconds). Some queries may timeout, but the cache will still be populated for successful ones.

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
