#!/usr/bin/env python3
"""
OWLERY Cache Reload Script

This script caches OWLERY queries for Virtual Fly Brain (VFB) by running all possible queries
with all potential anatomy IDs against the OWLERY server.
"""

import requests
import threading
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from vfb_connect import vfb

def run_query_type(name, url_template, ids, timeout, parallel, counter, counter_lock, total_queries):
    """Run all IDs for a single query type in its own thread pool."""
    with ThreadPoolExecutor(max_workers=parallel) as executor:
        futures = {executor.submit(run_query, name, url_template, id, timeout): id for id in ids}
        for future in as_completed(futures):
            result = future.result()
            with counter_lock:
                counter[0] += 1
                count = counter[0]
            print(f"[{count}/{total_queries}] {result}")

_thread_local = threading.local()

def _get_session():
    if not hasattr(_thread_local, 'session'):
        _thread_local.session = requests.Session()
    return _thread_local.session

def run_query(name, url_template, id, timeout=60):
    query_url = url_template.format(id=id)
    try:
        response = _get_session().get(query_url, timeout=timeout)
        if response.status_code == 200:
            return f"✓ {name} for {id}"
        else:
            return f"✗ {name} for {id}: status {response.status_code}"
    except Exception as e:
        return f"✗ {name} for {id}: {str(e)}"

# List of OWLERY queries extracted from the queries_execution_notebook.ipynb
# Each tuple: (name, url_template)
queries = [
    ("Owlery Neuron class with part here", "http://owl.virtualflybrain.org/kbs/vfb/subclasses?object=%3Chttp://purl.obolibrary.org/obo/FBbt_00005106%3E%20and%20%3Chttp://purl.obolibrary.org/obo/RO_0002131%3E%20some%20%3Chttp://purl.obolibrary.org/obo/{id}%3E&direct=false&includeDeprecated=false&includeEquivalent=true"),
    ("Owlery Neurons Presynaptic", "http://owl.virtualflybrain.org/kbs/vfb/subclasses?object=%3Chttp://purl.obolibrary.org/obo/FBbt_00005106%3E%20and%20%3Chttp://purl.obolibrary.org/obo/RO_0002113%3E%20some%20%3Chttp://purl.obolibrary.org/obo/{id}%3E&direct=false&includeDeprecated=false&includeEquivalent=true"),
    ("Owlery Neurons Postsynaptic", "http://owl.virtualflybrain.org/kbs/vfb/subclasses?object=%3Chttp://purl.obolibrary.org/obo/FBbt_00005106%3E%20and%20%3Chttp://purl.obolibrary.org/obo/RO_0002110%3E%20some%20%3Chttp://purl.obolibrary.org/obo/{id}%3E&direct=false&includeDeprecated=false&includeEquivalent=true"),
    ("Owlery Neuron classes fasciculating here", "http://owl.virtualflybrain.org/kbs/vfb/subclasses?object=%3Chttp://purl.obolibrary.org/obo/FBbt_00005099%3E%20and%20%3Chttp://purl.obolibrary.org/obo/RO_0002134%3E%20some%20%3Chttp://purl.obolibrary.org/obo/{id}%3E&direct=false&includeDeprecated=false&includeEquivalent=true"),
    ("Owlery Neuron classes with synaptic terminals here", "http://owl.virtualflybrain.org/kbs/vfb/subclasses?object=%3Chttp://purl.obolibrary.org/obo/FBbt_00005106%3E%20and%20(%20%3Chttp://purl.obolibrary.org/obo/RO_0002113%3E%20some%20%3Chttp://purl.obolibrary.org/obo/{id}%3E%20)&direct=false&includeDeprecated=false&includeEquivalent=false"),
    ("subClassOf cell overlaps some X", "http://owl.virtualflybrain.org/kbs/vfb/subclasses?object=%3Chttp://purl.obolibrary.org/obo/CL_0000000%3E%20and%20%3Chttp://purl.obolibrary.org/obo/RO_0002131%3E%20some%20%3Chttp://purl.obolibrary.org/obo/{id}%3E&direct=false&includeDeprecated=false&includeEquivalent=true"),
    ("Owlery Images of neurons with some part here", "http://owl.virtualflybrain.org/kbs/vfb/instances?object=%3Chttp://purl.obolibrary.org/obo/FBbt_00005106%3E%20and%20%3Chttp://purl.obolibrary.org/obo/RO_0002131%3E%20some%20%3Chttp://purl.obolibrary.org/obo/{id}%3E&direct=false&includeDeprecated=false"),
    ("Images of neurons that develops from this", "http://owl.virtualflybrain.org/kbs/vfb/instances?object=%3Chttp://purl.obolibrary.org/obo/FBbt_00005106%3E%20and%20%3Chttp://purl.obolibrary.org/obo/BFO_0000050%3E%20some%20%3Chttp://purl.obolibrary.org/obo/{id}%3E&direct=false&includeDeprecated=false"),
    ("V3 term info Queries", "https://v3-cached.virtualflybrain.org/get_term_info?id={id}"),
    ("V3 ListAllAvailableImages", "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=ListAllAvailableImages"),
    ("V3 PartsOf", "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=PartsOf"),
    ("V3 SubclassesOf", "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=SubclassesOf"),
    ("V3 NeuronInputsTo", "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=NeuronInputsTo"),
    ("V3 NeuronNeuronConnectivityQuery", "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=NeuronNeuronConnectivityQuery"),
    ("V3 NeuronsPartHere", "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=NeuronsPartHere"),
    ("V3 NeuronsSynaptic", "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=NeuronsSynaptic"),
    ("V3 PaintedDomains", "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=PaintedDomains"),
    ("V3 AllAlignedImages", "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=AllAlignedImages"),
    ("V3 AllDatasets", "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=AllDatasets"),
    ("V3 ExpressionOverlapsHere", "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=ExpressionOverlapsHere"),
    ("V3 SimilarMorphologyTo", "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=SimilarMorphologyTo"),
]

def main():
    parser = argparse.ArgumentParser(description='Cache OWLERY queries for VFB.')
    parser.add_argument('--max-ids', type=int, default=None, help='Maximum number of IDs to test per query (for testing).')
    parser.add_argument('--timeout', type=int, default=9000, help='Timeout in seconds for each query request.')
    parser.add_argument('--parallel', type=int, default=50, help='Number of parallel requests to run at once.')
    args = parser.parse_args()

    # Connect to VFB
    print("Connecting to VFB...")
    # vfb is already initialized

    # Get all anatomy class IDs
    print("Retrieving all anatomy class IDs...")
    id_query = "MATCH (n:Entity) WHERE exists(n.short_form) AND (n:Class OR n:Individual) AND NOT n.short_form starts with 'VFBc_' AND NOT n.short_form starts with 'VFB_internal' RETURN distinct n.short_form"
    ids_result = vfb.nc.commit_list([id_query])
    ids = [row['row'][0] for row in ids_result[0]['data']]

    vfb_ids = sorted([i for i in ids if i.startswith('VFB_')], reverse=True)
    fbbt_ids = sorted([i for i in ids if i.startswith('FBbt_')], reverse=True)
    other_ids = [i for i in ids if not i.startswith('VFB_') and not i.startswith('FBbt_')]
    ids = vfb_ids + fbbt_ids + other_ids
    print(f"Found {len(ids)} anatomy IDs.")

    if args.max_ids:
        ids = ids[:args.max_ids]
        print(f"Limited to first {args.max_ids} IDs for testing.")

    total_queries = len(queries) * len(ids)
    print(f"Total queries to run: {total_queries}")

    all_tasks = [(name, url_template, id) for id in ids for name, url_template in queries]

    count = 0
    with ThreadPoolExecutor(max_workers=args.parallel) as executor:
        futures = {executor.submit(run_query, name, url_template, id, args.timeout): (name, id)
                   for name, url_template, id in all_tasks}
        for future in as_completed(futures):
            result = future.result()
            count += 1
            print(f"[{count}/{total_queries}] {result}")

    print("Caching complete.")

if __name__ == "__main__":
    main()
