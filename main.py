#!/usr/bin/env python3
"""
OWLERY Cache Reload Script

This script caches OWLERY queries for Virtual Fly Brain (VFB) by running all possible queries
with all potential anatomy IDs against the OWLERY server.
"""

import requests
import time
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from vfb_connect import vfb

def run_query(name, url_template, id, timeout=60):
    query_url = url_template.format(id=id)
    try:
        response = requests.get(query_url, timeout=timeout)
        if response.status_code == 200:
            try:
                data = response.json()
                if 'instances' in query_url:
                    count = len(data.get('hasInstance', []))
                elif 'subclasses' in query_url:
                    count = len(data.get('superClassOf', []))
                else:
                    count = 0  # For other endpoints if any
                return f"✓ {name} for {id}: {count} results"
            except:
                return f"✓ {name} for {id}"
        else:
            return f"Error for {name} {id}: {query_url}, status {response.status_code}"
    except Exception as e:
        return f"Error for {name} {id}: {query_url}, {str(e)}"

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
    ("V3 term info Queries", "https://v3-cached.virtualflybrain.org/get_term_info?id={id}")
]

def main():
    parser = argparse.ArgumentParser(description='Cache OWLERY queries for VFB.')
    parser.add_argument('--max-ids', type=int, default=None, help='Maximum number of IDs to test per query (for testing).')
    parser.add_argument('--timeout', type=int, default=60, help='Timeout in seconds for each query request.')
    parser.add_argument('--parallel', type=int, default=9, help='Number of parallel requests to run at once.')
    args = parser.parse_args()

    # Connect to VFB
    print("Connecting to VFB...")
    # vfb is already initialized

    # Get all anatomy class IDs
    print("Retrieving all anatomy class IDs...")
    id_query = "MATCH (n:Class) WHERE n.short_form STARTS WITH 'FBbt' RETURN n.short_form"
    ids_result = vfb.nc.commit_list([id_query])
    ids = [row['row'][0] for row in ids_result[0]['data']]
    ids.sort(reverse=True)  # Sort IDs in descending order to handle newest ones first
    print(f"Found {len(ids)} anatomy IDs.")

    if args.max_ids:
        ids = ids[:args.max_ids]
        print(f"Limited to first {args.max_ids} IDs for testing.")

    total_queries = len(queries) * len(ids)
    print(f"Total queries to run: {total_queries}")

    count = 0
    for id in ids:
        print(f"Processing ID: {id}")
        with ThreadPoolExecutor(max_workers=args.parallel) as executor:
            futures = [executor.submit(run_query, name, url_template, id, args.timeout) for name, url_template in queries]
            for future in as_completed(futures):
                result = future.result()
                count += 1
                print(f"[{count}/{total_queries}] {result}")
                time.sleep(0.1)  # Small delay between prints

    print("Caching complete.")

if __name__ == "__main__":
    main()