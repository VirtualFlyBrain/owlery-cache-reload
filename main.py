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
    if id is None:
        query_url = url_template
        id_label = "(global)"
    else:
        query_url = url_template.format(id=id)
        id_label = id

    try:
        response = _get_session().get(query_url, timeout=timeout)
        if response.status_code == 200:
            return f"✓ {name} for {id_label}"
        else:
            return f"✗ {name} for {id_label}: status {response.status_code}"
    except Exception as e:
        return f"✗ {name} for {id_label}: {str(e)}"

# New script queries and target filtering based on node labels/supertypes.
# Each query is: name, url_template, id_required, id_filter(id, labels)->bool

def _filter_any(_id, _labels):
    return True

def _filter_template_individual(_id, labels):
    return "Template" in labels and "Individual" in labels

def _filter_class_or_individual(_id, labels):
    return "Class" in labels or "Individual" in labels

def _filter_neuron(_id, labels):
    return "Neuron" in labels

def _filter_class_only(_id, labels):
    return "Class" in labels

def _filter_class_anatomy(_id, labels):
    # Allow anatomy IDs that are either Class or Individual to avoid filtering out valid anatomy terms.
    result = ("Class" in labels or "Individual" in labels) and "Anatomy" in labels
    if _id == "VFB_x0000009":
        print(f"[DEBUG] _filter_class_anatomy({_id}) labels={labels} => {result}")
    return result

def _filter_feature_id(id, labels):
    # Only run this query on FBco IDs (feature combination terms), not all Feature-labeled nodes.
    return id.startswith("FBco_")

queries = [
    # legacy OWLERY queries (per-term, each ID is used)
    {"name": "Owlery Neuron class with part here", "template": "http://owl.virtualflybrain.org/kbs/vfb/subclasses?object=%3Chttp://purl.obolibrary.org/obo/FBbt_00005106%3E%20and%20%3Chttp://purl.obolibrary.org/obo/RO_0002131%3E%20some%20%3Chttp://purl.obolibrary.org/obo/{id}%3E&direct=false&includeDeprecated=false&includeEquivalent=true", "id_required": True, "id_filter": _filter_class_anatomy, "allow_fallback": False},
    {"name": "Owlery Neurons Presynaptic", "template": "http://owl.virtualflybrain.org/kbs/vfb/subclasses?object=%3Chttp://purl.obolibrary.org/obo/FBbt_00005106%3E%20and%20%3Chttp://purl.obolibrary.org/obo/RO_0002113%3E%20some%20%3Chttp://purl.obolibrary.org/obo/{id}%3E&direct=false&includeDeprecated=false&includeEquivalent=true", "id_required": True, "id_filter": _filter_class_anatomy, "allow_fallback": False},
    {"name": "Owlery Neurons Postsynaptic", "template": "http://owl.virtualflybrain.org/kbs/vfb/subclasses?object=%3Chttp://purl.obolibrary.org/obo/FBbt_00005106%3E%20and%20%3Chttp://purl.obolibrary.org/obo/RO_0002110%3E%20some%20%3Chttp://purl.obolibrary.org/obo/{id}%3E&direct=false&includeDeprecated=false&includeEquivalent=true", "id_required": True, "id_filter": _filter_class_anatomy, "allow_fallback": False},
    {"name": "Owlery Neuron classes fasciculating here", "template": "http://owl.virtualflybrain.org/kbs/vfb/subclasses?object=%3Chttp://purl.obolibrary.org/obo/FBbt_00005099%3E%20and%20%3Chttp://purl.obolibrary.org/obo/RO_0002134%3E%20some%20%3Chttp://purl.obolibrary.org/obo/{id}%3E&direct=false&includeDeprecated=false&includeEquivalent=true", "id_required": True, "id_filter": _filter_class_anatomy, "allow_fallback": False},
    {"name": "Owlery Neuron classes with synaptic terminals here", "template": "http://owl.virtualflybrain.org/kbs/vfb/subclasses?object=%3Chttp://purl.obolibrary.org/obo/FBbt_00005106%3E%20and%20(%20%3Chttp://purl.obolibrary.org/obo/RO_0002113%3E%20some%20%3Chttp://purl.obolibrary.org/obo/{id}%3E%20)&direct=false&includeDeprecated=false&includeEquivalent=false", "id_required": True, "id_filter": _filter_class_anatomy, "allow_fallback": False},
    {"name": "Owlery SubclassesOf", "template": "http://owl.virtualflybrain.org/kbs/vfb/subclasses?object=%3Chttp://purl.obolibrary.org/obo/{id}%3E&direct=false&includeDeprecated=false&includeEquivalent=true", "id_required": True, "id_filter": _filter_class_only, "allow_fallback": False},
    {"name": "Owlery Part of", "template": "http://owl.virtualflybrain.org/kbs/vfb/subclasses?object=%3Chttp://purl.obolibrary.org/obo/BFO_0000050%3E%20some%20%3Chttp://purl.obolibrary.org/obo/{id}%3E&direct=false&includeDeprecated=false&includeEquivalent=true", "id_required": True, "id_filter": _filter_class_only, "allow_fallback": False},
    {"name": "subClassOf cell overlaps some X", "template": "http://owl.virtualflybrain.org/kbs/vfb/subclasses?object=%3Chttp://purl.obolibrary.org/obo/CL_0000000%3E%20and%20%3Chttp://purl.obolibrary.org/obo/RO_0002131%3E%20some%20%3Chttp://purl.obolibrary.org/obo/{id}%3E&direct=false&includeDeprecated=false&includeEquivalent=true", "id_required": True, "id_filter": _filter_class_anatomy},
    {"name": "Owlery Images of neurons with some part here", "template": "http://owl.virtualflybrain.org/kbs/vfb/instances?object=%3Chttp://purl.obolibrary.org/obo/FBbt_00005106%3E%20and%20%3Chttp://purl.obolibrary.org/obo/RO_0002131%3E%20some%20%3Chttp://purl.obolibrary.org/obo/{id}%3E&direct=false&includeDeprecated=false", "id_required": True, "id_filter": _filter_class_anatomy, "allow_fallback": False},
    {"name": "Images of neurons that develops from this", "template": "http://owl.virtualflybrain.org/kbs/vfb/instances?object=%3Chttp://purl.obolibrary.org/obo/FBbt_00005106%3E%20and%20%3Chttp://purl.obolibrary.org/obo/BFO_0000050%3E%20some%20%3Chttp://purl.obolibrary.org/obo/{id}%3E&direct=false&includeDeprecated=false", "id_required": True, "id_filter": _filter_class_anatomy},

    # V3 cached endpoints by query_type (per-term)
    {"name": "V3 term info", "template": "https://v3-cached.virtualflybrain.org/get_term_info?id={id}", "id_required": True, "id_filter": _filter_any},
    {"name": "V3 ListAllAvailableImages", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=ListAllAvailableImages", "id_required": True, "id_filter": _filter_template_individual},
    {"name": "V3 PartsOf", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=PartsOf", "id_required": True, "id_filter": _filter_class_or_individual},
    {"name": "V3 SubclassesOf", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=SubclassesOf", "id_required": True, "id_filter": _filter_class_or_individual},
    {"name": "V3 NeuronInputsTo", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=NeuronInputsTo", "id_required": True, "id_filter": _filter_neuron},
    {"name": "V3 NeuronNeuronConnectivityQuery", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=NeuronNeuronConnectivityQuery", "id_required": True, "id_filter": _filter_neuron},
    {"name": "V3 NeuronsPartHere", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=NeuronsPartHere", "id_required": True, "id_filter": _filter_neuron},
    {"name": "V3 NeuronsSynaptic", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=NeuronsSynaptic", "id_required": True, "id_filter": _filter_neuron},
    {"name": "V3 PaintedDomains", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=PaintedDomains", "id_required": True, "id_filter": _filter_template_individual},
    {"name": "V3 AllAlignedImages", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=AllAlignedImages", "id_required": True, "id_filter": _filter_template_individual},
    {"name": "V3 AllDatasets", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=AllDatasets", "id_required": True, "id_filter": _filter_template_individual},
    {"name": "V3 ExpressionOverlapsHere", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=ExpressionOverlapsHere", "id_required": True, "id_filter": _filter_class_or_individual},
    {"name": "V3 SimilarMorphologyTo", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=SimilarMorphologyTo", "id_required": True, "id_filter": _filter_class_or_individual},

    # Newly requested V3 endpoints from VFBquery release
    {"name": "V3 resolve_entity", "template": "https://v3-cached.virtualflybrain.org/resolve_entity?query={id}", "id_required": True, "id_filter": _filter_any},
    {"name": "V3 find_stocks", "template": "https://v3-cached.virtualflybrain.org/find_stocks?id={id}&collection=all", "id_required": True, "id_filter": _filter_any},
    {"name": "V3 resolve_combination", "template": "https://v3-cached.virtualflybrain.org/resolve_combination?query={id}", "id_required": True, "id_filter": _filter_any},
    {"name": "V3 find_combo_publications", "template": "https://v3-cached.virtualflybrain.org/find_combo_publications?id={id}", "id_required": True, "id_filter": _filter_feature_id},

    # Non-ID, global cached endpoints
    {"name": "V3 list_connectome_datasets", "template": "https://v3-cached.virtualflybrain.org/list_connectome_datasets", "id_required": False, "id_filter": _filter_any},
    {"name": "V3 query_connectivity", "template": "https://v3-cached.virtualflybrain.org/query_connectivity?upstream_type=FBbt_00005093&downstream_type=FBbt_00005093&weight=5&group_by_class=false&exclude_dbs=hb,fafb", "id_required": False, "id_filter": _filter_any},
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

    # Get all relevant Entity IDs and labels
    print("Retrieving terms + labels from Neo4j...")
    id_query = "MATCH (n:Entity) WHERE exists(n.short_form) AND NOT n.short_form starts with 'VFBc_' AND NOT n.short_form starts with 'VFB_internal' RETURN distinct n.short_form AS id, labels(n) AS labels"
    ids_result = vfb.nc.commit_list([id_query])
    id_labels_map = {row['row'][0]: row['row'][1] for row in ids_result[0]['data']}

    # Preserve current sort order logic for initial groups
    vfb_ids = sorted([i for i in id_labels_map.keys() if i.startswith('VFB_')], reverse=True)
    fbbt_ids = sorted([i for i in id_labels_map.keys() if i.startswith('FBbt_')], reverse=True)
    other_ids = [i for i in id_labels_map.keys() if not i.startswith('VFB_') and not i.startswith('FBbt_')]
    all_entity_ids = vfb_ids + fbbt_ids + other_ids

    # Debug and sanity check for known case where label inference is asked
    suspect_id = 'VFB_x0000009'
    if suspect_id in id_labels_map:
        suspect_labels = id_labels_map[suspect_id]
        print(f"[DEBUG] suspect {suspect_id} labels={suspect_labels}")
        for q in queries:
            fid = q.get('id_filter', _filter_any)
            print(f"[DEBUG]   {q['name']} -> {fid(suspect_id, suspect_labels)}")

    # Filter by super-types/labels to avoid useless term churn
    valid_supertypes = {"Entity", "Anatomy", "Class", "Individual", "Neuron", "Cell", "Template", "VFB", "Nervous_system"}
    filtered_ids = [i for i in all_entity_ids if set(id_labels_map.get(i, [])).intersection(valid_supertypes)]
    print(f"Found {len(filtered_ids)} eligible IDs after label filtering (from {len(all_entity_ids)} candidate entity IDs).")

    # Enforce --max-ids at query-selection time (instead of truncating the base pools)
    if args.max_ids:
        print(f"Limited to first {args.max_ids} IDs per query candidate list for testing.")

    # Build query jobs optimized by ID requirement and ID filter
    query_jobs = []
    for q in queries:
        if q.get("id_required", True):
            candidate_ids = [i for i in filtered_ids if q.get("id_filter", _filter_any)(i, id_labels_map.get(i, []))]
            if args.max_ids:
                candidate_ids = candidate_ids[:args.max_ids]
            allow_fb = q.get("allow_fallback", False)
            if not candidate_ids:
                if allow_fb:
                    # Fallback: if ID filter produces no matches, run on all Entity IDs.
                    candidate_ids = all_entity_ids[:args.max_ids] if args.max_ids else all_entity_ids
                    print(f"Warning: no specific IDs matched for query '{q['name']}', falling back to {len(candidate_ids)} Entity term IDs")
                else:
                    print(f"Skipping query '{q['name']}' (strict class-based filter, no matches)")
                    continue
            query_jobs.append((q["name"], q["template"], candidate_ids))
        else:
            query_jobs.append((q["name"], q["template"], [None]))

    total_queries = sum(len(ids) for _name, _template, ids in query_jobs)
    print(f"Total queries to run: {total_queries} across {len(query_jobs)} query types.")

    counter = [0]
    counter_lock = threading.Lock()

    # Each query type gets its own thread pool; all pools run concurrently.
    with ThreadPoolExecutor(max_workers=len(query_jobs)) as query_type_executor:
        futures = [
            query_type_executor.submit(
                run_query_type, name, url_template, ids,
                args.timeout, args.parallel, counter, counter_lock, total_queries
            )
            for name, url_template, ids in query_jobs
        ]
        for future in as_completed(futures):
            future.result()  # re-raise any exceptions

    print("Caching complete.")

if __name__ == "__main__":
    main()
