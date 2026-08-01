#!/usr/bin/env python3
"""
OWLERY Cache Reload Script

This script caches OWLERY queries for Virtual Fly Brain (VFB) by running all possible queries
with all potential anatomy IDs against the OWLERY server.
"""

import time
import requests
import threading
import argparse
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from vfb_connect import vfb

from throttle import AdaptiveLimiter, StatusGovernor, backoff_delay

# Statuses that mean "the service is busy, come back later" rather than "this
# query is wrong". A 503 from VFBquery specifically means the result is still
# being computed and *will* be cached, so retrying is what populates the cache.
RETRYABLE_STATUSES = (429, 502, 503, 504)

def run_query_type(name, url_template, ids, timeout, threads, counter, counter_lock,
                   total_queries, headers=None, limiter=None, retries=4,
                   backoff_base=15.0, backoff_cap=300.0):
    """Run all IDs for a single query type.

    The pool size here only bounds how many threads may be *waiting*; the
    limiter decides how many requests are actually in flight across the whole
    sweep, so adding query types no longer multiplies the load on the service.
    """
    with ThreadPoolExecutor(max_workers=threads) as executor:
        futures = {
            executor.submit(run_query, name, url_template, id, timeout, headers,
                            limiter, retries, backoff_base, backoff_cap): id
            for id in ids
        }
        for future in as_completed(futures):
            result = future.result()
            with counter_lock:
                counter[0] += 1
                count = counter[0]
            print(f"[{count}/{total_queries}] {result}", flush=True)

_thread_local = threading.local()

def _get_session():
    if not hasattr(_thread_local, 'session'):
        _thread_local.session = requests.Session()
    return _thread_local.session

def _retry_after_seconds(response, fallback):
    """Honour a Retry-After header if the service sends one."""
    value = response.headers.get("Retry-After")
    if not value:
        return fallback
    try:
        return max(0.0, float(value.strip()))
    except (TypeError, ValueError):
        return fallback


def run_query(name, url_template, id, timeout=200, headers=None, limiter=None,
              retries=4, backoff_base=15.0, backoff_cap=300.0):
    if id is None:
        query_url = url_template
        id_label = "(global)"
    else:
        query_url = url_template.format(id=id)
        id_label = id

    last_reason = "no attempt made"

    for attempt in range(retries + 1):
        # The slot is held only for the request itself: a thread that is
        # backing off must not occupy capacity it is not using.
        if limiter is not None and not limiter.acquire():
            return f"✗ {name} for {id_label}: aborted before request"
        try:
            response = _get_session().get(query_url, timeout=timeout, headers=headers)
        except Exception as e:
            response = None
            last_reason = f"{type(e).__name__}: {e}"
        finally:
            if limiter is not None:
                limiter.release()

        if response is not None:
            if response.status_code == 200:
                suffix = f" (after {attempt} retr{'y' if attempt == 1 else 'ies'})" if attempt else ""
                return f"✓ {name} for {id_label}{suffix}"
            if response.status_code not in RETRYABLE_STATUSES:
                # A real error with this query -- retrying will not help.
                return f"✗ {name} for {id_label}: status {response.status_code}"
            last_reason = f"status {response.status_code}"

        if attempt == retries:
            break

        delay = backoff_delay(attempt, backoff_base, backoff_cap)
        if response is not None:
            delay = _retry_after_seconds(response, delay)
        time.sleep(delay)

    return f"✗ {name} for {id_label}: {last_reason} (gave up after {retries} retries)"

# New script queries and target filtering based on node labels/supertypes.
# Each query is: name, url_template, id_required, id_filter(id, labels)->bool

def _filter_any(_id, _labels):
    return True

def _filter_template_individual(_id, labels):
    return "Template" in labels and "Individual" in labels

def _filter_class_only(_id, labels):
    return "Class" in labels

def _filter_class_anatomy(_id, labels):
    return "Class" in labels and "Anatomy" in labels

def _filter_individual_neuron(id, labels):
    """Individual neuron images - for morphology comparison."""
    return "Individual" in labels and "Neuron" in labels

def _filter_connected_neuron(id, labels):
    """Individual neurons with connectivity data."""
    return "Individual" in labels and "Neuron" in labels and "has_neuron_connectivity" in labels

def _filter_flybase_id(id, labels):
    """FlyBase entity IDs only (for FlyBase PostgreSQL queries)."""
    return id.startswith("FB")

def _filter_flybase_stocks(id, labels):
    """IDs valid for find_stocks: FBgn, FBal, FBti, FBco, FBst."""
    return id[:4] in ("FBgn", "FBal", "FBti", "FBco", "FBst") if len(id) >= 4 else False

def _filter_feature_id(id, labels):
    # Only run this query on FBco IDs (feature combination terms), not all Feature-labeled nodes.
    return id.startswith("FBco_")

# Filters for the v2-frontend query types (vfb.xmi CompoundRefQuery matchingCriteria).
# Each mirrors the input-type contract the v2 UI uses to decide whether to offer the
# query, so the precache warms every term that can trigger a cold start. Where the
# matchingCriteria discriminate on an Anatomy subtype (Synaptic_neuropil, Ganglion,
# Neuromere, ...) we use the Class & Anatomy superset: warming a few extra slots is
# cheap, missing a cold start is not.

def _filter_class_neuron(id, labels):
    """Neuron classes - class-level up/downstream connectivity."""
    return "Class" in labels and "Neuron" in labels

def _filter_region_connectivity(id, labels):
    """Individuals with per-region connectivity data."""
    return "has_region_connectivity" in labels

def _filter_dataset_images(id, labels):
    """Datasets that contain images (DatasetImages)."""
    return "DataSet" in labels and "has_image" in labels

def _filter_dataset_scrnaseq(id, labels):
    """Datasets with single-cell RNAseq results (scRNAdatasetData)."""
    return "DataSet" in labels and "hasScRNAseq" in labels

def _filter_anat_scrnaseq(id, labels):
    """Anatomy classes with single-cell RNAseq results (anatScRNAseqQuery)."""
    return "Class" in labels and "Anatomy" in labels and "hasScRNAseq" in labels

def _filter_gene_scrnaseq(id, labels):
    """Gene classes with single-cell RNAseq results (expressionCluster)."""
    return "Class" in labels and "Gene" in labels and "hasScRNAseq" in labels

def _filter_cluster(id, labels):
    """scRNAseq cluster individuals (clusterExpression)."""
    return "Individual" in labels and "Cluster" in labels

def _filter_pub(id, labels):
    """Publication individuals (TermsForPub)."""
    return "Individual" in labels and "pub" in labels

def _filter_nblast_neuron(id, labels):
    """Individual neurons with NBLAST-to-exp results (SimilarMorphologyToPartOf)."""
    return "Individual" in labels and "Neuron" in labels and "NBLASTexp" in labels

def _filter_nblast_exp(id, labels):
    """Expression-pattern individuals with NBLAST-to-exp results (SimilarMorphologyToPartOfexp)."""
    return ("Individual" in labels and "NBLASTexp" in labels
            and ("Expression_pattern" in labels or "Expression_pattern_fragment" in labels))

def _filter_nb_exp(id, labels):
    """Expression-pattern individuals with NeuronBridge results (SimilarMorphologyToNB)."""
    return ("Individual" in labels and "neuronbridge" in labels
            and ("Expression_pattern" in labels or "Expression_pattern_fragment" in labels))

def _filter_nb_neuron(id, labels):
    """Individual neurons with NeuronBridge results (SimilarMorphologyToNBexp)."""
    return "Individual" in labels and "neuronbridge" in labels and "Neuron" in labels

def _server_of(url_template):
    """Derive the backend host a query targets, used by --only/--skip.

    Returns the netloc (e.g. 'owl.virtualflybrain.org' or
    'v3-cached.virtualflybrain.org'). Falls back to '' for malformed templates.
    """
    return (urlparse(url_template).netloc or "").lower()

def _query_matches(q, tokens):
    """True if any token matches the query's server host or name (case-insensitive
    substring), or one of its explicit tags (exact, case-insensitive). Used to
    resolve both --only and --skip."""
    server = _server_of(q["template"])
    name = q["name"].lower()
    tags = [t.lower() for t in q.get("tags", [])]
    return any(t in server or t in name or t in tags for t in tokens)

def _parse_tokens(raw):
    """Split a comma-separated CLI value into a list of lowercased tokens."""
    if not raw:
        return []
    return [t.strip().lower() for t in raw.split(",") if t.strip()]

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
    {"name": "V3 ListAllAvailableImages", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=ListAllAvailableImages", "id_required": True, "id_filter": _filter_class_anatomy},
    {"name": "V3 PartsOf", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=PartsOf", "id_required": True, "id_filter": _filter_class_only},
    {"name": "V3 SubclassesOf", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=SubclassesOf", "id_required": True, "id_filter": _filter_class_only},
    {"name": "V3 NeuronInputsTo", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=NeuronInputsTo", "id_required": True, "id_filter": _filter_connected_neuron},
    {"name": "V3 NeuronNeuronConnectivityQuery", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=NeuronNeuronConnectivityQuery", "id_required": True, "id_filter": _filter_connected_neuron},
    {"name": "V3 NeuronsPartHere", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=NeuronsPartHere", "id_required": True, "id_filter": _filter_class_anatomy},
    {"name": "V3 NeuronsSynaptic", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=NeuronsSynaptic", "id_required": True, "id_filter": _filter_class_anatomy},
    {"name": "V3 PaintedDomains", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=PaintedDomains", "id_required": True, "id_filter": _filter_template_individual},
    {"name": "V3 AllAlignedImages", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=AllAlignedImages", "id_required": True, "id_filter": _filter_template_individual},
    {"name": "V3 AllDatasets", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=AllDatasets", "id_required": True, "id_filter": _filter_template_individual},
    # NB: query_type=ExpressionOverlapsHere removed - the VFBquery run_query endpoint
    # does not recognise it (returns HTTP 400) and it is not in vfb.xmi. Its live
    # equivalents are TransgeneExpressionHere and AnatomyExpressedIn (added below).
    {"name": "V3 SimilarMorphologyTo", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=SimilarMorphologyTo", "id_required": True, "id_filter": _filter_individual_neuron, "tags": ["morphology"]},

    # V3 cached query_types offered by the v2 frontend (vfb.xmi) that were previously
    # not pre-warmed, so cold starts hit users on first request after a release.
    # Filters mirror each query's matchingCriteria. See geppetto-vfb/model/vfb.xmi.
    # -- class-level anatomy queries (Class & Anatomy superset) --
    {"name": "V3 TransgeneExpressionHere", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=TransgeneExpressionHere", "id_required": True, "id_filter": _filter_class_anatomy, "tags": ["expression"]},
    {"name": "V3 AnatomyExpressedIn", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=AnatomyExpressedIn", "id_required": True, "id_filter": _filter_class_anatomy, "tags": ["expression"]},
    {"name": "V3 epFrag", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=epFrag", "id_required": True, "id_filter": _filter_class_anatomy, "tags": ["expression"]},
    {"name": "V3 NeuronClassesFasciculatingHere", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=NeuronClassesFasciculatingHere", "id_required": True, "id_filter": _filter_class_anatomy},
    {"name": "V3 ImagesNeurons", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=ImagesNeurons", "id_required": True, "id_filter": _filter_class_anatomy},
    {"name": "V3 NeuronsPresynapticHere", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=NeuronsPresynapticHere", "id_required": True, "id_filter": _filter_class_anatomy},
    {"name": "V3 NeuronsPostsynapticHere", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=NeuronsPostsynapticHere", "id_required": True, "id_filter": _filter_class_anatomy},
    {"name": "V3 TractsNervesInnervatingHere", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=TractsNervesInnervatingHere", "id_required": True, "id_filter": _filter_class_anatomy},
    {"name": "V3 ComponentsOf", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=ComponentsOf", "id_required": True, "id_filter": _filter_class_anatomy},
    {"name": "V3 LineageClonesIn", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=LineageClonesIn", "id_required": True, "id_filter": _filter_class_anatomy},
    {"name": "V3 ImagesThatDevelopFrom", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=ImagesThatDevelopFrom", "id_required": True, "id_filter": _filter_class_anatomy},
    # -- class-level connectivity (Class & Neuron) --
    {"name": "V3 UpstreamClassConnectivity", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=UpstreamClassConnectivity", "id_required": True, "id_filter": _filter_class_neuron, "tags": ["connectivity"]},
    {"name": "V3 DownstreamClassConnectivity", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=DownstreamClassConnectivity", "id_required": True, "id_filter": _filter_class_neuron, "tags": ["connectivity"]},
    # -- individual neuron-to-region connectivity --
    {"name": "V3 NeuronRegionConnectivityQuery", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=NeuronRegionConnectivityQuery", "id_required": True, "id_filter": _filter_region_connectivity, "tags": ["connectivity"]},
    # -- datasets --
    {"name": "V3 DatasetImages", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=DatasetImages", "id_required": True, "id_filter": _filter_dataset_images, "tags": ["dataset"]},
    {"name": "V3 AlignedDatasets", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=AlignedDatasets", "id_required": True, "id_filter": _filter_template_individual, "tags": ["dataset"]},
    # -- publications --
    {"name": "V3 TermsForPub", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=TermsForPub", "id_required": True, "id_filter": _filter_pub, "tags": ["pub"]},
    # -- NBLAST / NeuronBridge morphology --
    {"name": "V3 SimilarMorphologyToPartOf", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=SimilarMorphologyToPartOf", "id_required": True, "id_filter": _filter_nblast_neuron, "tags": ["morphology", "nblast"]},
    {"name": "V3 SimilarMorphologyToPartOfexp", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=SimilarMorphologyToPartOfexp", "id_required": True, "id_filter": _filter_nblast_exp, "tags": ["morphology", "nblast"]},
    {"name": "V3 SimilarMorphologyToNB", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=SimilarMorphologyToNB", "id_required": True, "id_filter": _filter_nb_exp, "tags": ["morphology", "neuronbridge"]},
    {"name": "V3 SimilarMorphologyToNBexp", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=SimilarMorphologyToNBexp", "id_required": True, "id_filter": _filter_nb_neuron, "tags": ["morphology", "neuronbridge"]},
    # -- single-cell RNAseq --
    {"name": "V3 anatScRNAseqQuery", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=anatScRNAseqQuery", "id_required": True, "id_filter": _filter_anat_scrnaseq, "tags": ["scrnaseq"]},
    {"name": "V3 clusterExpression", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=clusterExpression", "id_required": True, "id_filter": _filter_cluster, "tags": ["scrnaseq"]},
    {"name": "V3 scRNAdatasetData", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=scRNAdatasetData", "id_required": True, "id_filter": _filter_dataset_scrnaseq, "tags": ["scrnaseq"]},
    {"name": "V3 expressionCluster", "template": "https://v3-cached.virtualflybrain.org/run_query?id={id}&query_type=expressionCluster", "id_required": True, "id_filter": _filter_gene_scrnaseq, "tags": ["scrnaseq"]},

    # Newly requested V3 endpoints from VFBquery release
    {"name": "V3 resolve_entity", "template": "https://v3-cached.virtualflybrain.org/resolve_entity?query={id}", "id_required": True, "id_filter": _filter_flybase_id},
    {"name": "V3 find_stocks", "template": "https://v3-cached.virtualflybrain.org/find_stocks?id={id}&collection=all", "id_required": True, "id_filter": _filter_flybase_stocks, "tags": ["flybase", "stocks"]},
    {"name": "V3 resolve_combination", "template": "https://v3-cached.virtualflybrain.org/resolve_combination?query={id}", "id_required": True, "id_filter": _filter_feature_id},
    {"name": "V3 find_combo_publications", "template": "https://v3-cached.virtualflybrain.org/find_combo_publications?id={id}", "id_required": True, "id_filter": _filter_feature_id},

    # Non-ID, global cached endpoints
    {"name": "V3 list_connectome_datasets", "template": "https://v3-cached.virtualflybrain.org/list_connectome_datasets", "id_required": False, "id_filter": _filter_any},
    {"name": "V3 query_connectivity", "template": "https://v3-cached.virtualflybrain.org/query_connectivity?upstream_type=FBbt_00005093&downstream_type=FBbt_00005093&weight=5&group_by_class=false&exclude_dbs=hb,fafb", "id_required": False, "id_filter": _filter_any},
]

def main():
    parser = argparse.ArgumentParser(description='Cache OWLERY queries for VFB.')
    parser.add_argument('--max-ids', type=int, default=None, help='Maximum number of IDs to test per query (for testing).')
    parser.add_argument(
        '--timeout', type=int, default=200,
        help='Per-request timeout in seconds (default 200, just past the '
             "server's 180s response budget). A thread that waits longer than "
             'the server is ever going to take is holding a connection open '
             'for nothing.',
    )
    parser.add_argument(
        '--parallel', type=int, default=4,
        help='Ceiling on requests in flight across the WHOLE sweep (default 4). '
             'This is a global cap, not a per-query-type one: adding query '
             'types no longer multiplies the load on the service. The governor '
             'starts at --start-parallel and only creeps up towards this '
             'ceiling while the service is idle.',
    )
    parser.add_argument(
        '--start-parallel', type=int, default=1,
        help='Requests in flight to begin with, before the governor has seen '
             'any status readings (default 1).',
    )
    parser.add_argument(
        '--threads-per-query', type=int, default=4,
        help='Worker threads per query type (default 4). These only bound how '
             'many requests may be queued up ready to go; --parallel decides '
             'how many are actually in flight.',
    )
    parser.add_argument(
        '--status-url', default='https://vfbquery.virtualflybrain.org/status',
        help='VFBquery status endpoint used to pace the sweep. The governor '
             'backs off whenever anything is queued there and pauses outright '
             'when the service is saturated, so live user traffic always takes '
             'priority. Pass an empty string to disable and run at a fixed '
             '--parallel.',
    )
    parser.add_argument(
        '--poll-interval', type=float, default=10.0,
        help='Seconds between status polls (default 10).',
    )
    parser.add_argument(
        '--idle-fraction', type=float, default=0.25,
        help='Fraction of the service\'s max_concurrent below which it counts '
             'as idle enough to speed up (default 0.25).',
    )
    parser.add_argument(
        '--pause-seconds', type=float, default=60.0,
        help='How long to stand down completely after finding the service '
             'saturated (default 60).',
    )
    parser.add_argument(
        '--retries', type=int, default=4,
        help='Retries for busy responses (429/502/503/504) before giving up on '
             'an ID (default 4). A 503 means the result is still computing and '
             'will be cached, so retrying is how the cache actually gets '
             'populated.',
    )
    parser.add_argument(
        '--backoff-base', type=float, default=15.0,
        help='First retry delay in seconds; doubles per attempt with jitter '
             '(default 15).',
    )
    parser.add_argument(
        '--backoff-cap', type=float, default=300.0,
        help='Maximum retry delay in seconds (default 300).',
    )
    parser.add_argument(
        '--force-refresh', action='store_true',
        help='Send X-Force-Refresh: true on every request. owl_cache (v3-cached) '
             'bypasses its cache for the request and overwrites the canonical slot '
             'with the fresh upstream response. Use after a VFBquery release to '
             'pre-warm the cache so end-users never see a cold miss.',
    )
    parser.add_argument(
        '--only', default=None, metavar='TOKENS',
        help='Comma-separated list of tokens; only run query types whose backend '
             'server host OR name contains one of them (case-insensitive '
             'substring). E.g. --only owl (just OWLERY), '
             '--only v3-cached (just the V3 cache), --only NeuronInputsTo. '
             'Applied before --skip.',
    )
    parser.add_argument(
        '--skip', default=None, metavar='TOKENS',
        help='Comma-separated list of tokens; skip query types whose backend '
             'server host OR name contains one of them (case-insensitive '
             'substring). E.g. --skip owl to refresh only the V3 cache and leave '
             'OWLERY alone. Applied after --only.',
    )
    parser.add_argument(
        '--list-servers', action='store_true',
        help='Print the backend servers and the query types targeting each, then exit. '
             'Use to see what tokens --only/--skip will match.',
    )
    args = parser.parse_args()

    only_tokens = _parse_tokens(args.only)
    skip_tokens = _parse_tokens(args.skip)

    # --list-servers: report the server -> query-type map and exit before any work.
    if args.list_servers:
        servers = {}
        for q in queries:
            servers.setdefault(_server_of(q["template"]), []).append(q["name"])
        for server in sorted(servers):
            print(f"{server} ({len(servers[server])} query types):")
            for qname in servers[server]:
                print(f"    {qname}")
        tags = {}
        for q in queries:
            for t in q.get("tags", []):
                tags.setdefault(t.lower(), []).append(q["name"])
        if tags:
            print("\nTags (usable as --only/--skip tokens):")
            for tag in sorted(tags):
                print(f"    {tag}: {', '.join(tags[tag])}")
        return

    # Resolve which query types to run from --only / --skip.
    selected_queries = queries
    if only_tokens:
        selected_queries = [q for q in selected_queries if _query_matches(q, only_tokens)]
    if skip_tokens:
        selected_queries = [q for q in selected_queries if not _query_matches(q, skip_tokens)]

    if only_tokens or skip_tokens:
        if only_tokens:
            print(f"--only {only_tokens}")
        if skip_tokens:
            print(f"--skip {skip_tokens}")
        print(f"Selected {len(selected_queries)} of {len(queries)} query types:")
        for q in selected_queries:
            print(f"    [{_server_of(q['template'])}] {q['name']}")
    if not selected_queries:
        print("No query types selected after applying --only/--skip; nothing to do.")
        return

    request_headers = {'X-Force-Refresh': 'true'} if args.force_refresh else None
    if args.force_refresh:
        print("force-refresh mode: X-Force-Refresh: true on every request")

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

    # Filter by super-types/labels to avoid useless term churn
    valid_supertypes = {"Entity", "Anatomy", "Class", "Individual", "Neuron", "Cell", "Template", "VFB", "Nervous_system", "has_neuron_connectivity", "Feature"}
    filtered_ids = [i for i in all_entity_ids if set(id_labels_map.get(i, [])).intersection(valid_supertypes)]
    print(f"Found {len(filtered_ids)} eligible IDs after label filtering (from {len(all_entity_ids)} candidate entity IDs).")

    # Enforce --max-ids at query-selection time (instead of truncating the base pools)
    if args.max_ids:
        print(f"Limited to first {args.max_ids} IDs per query candidate list for testing.")

    # Build query jobs optimized by ID requirement and ID filter
    query_jobs = []
    for q in selected_queries:
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

    # One global limiter shared by every query type. Previously each query type
    # got its own pool of --parallel threads and all pools ran at once, so the
    # sweep could offer the service len(query_jobs) x parallel requests --
    # thousands, against a service advertising max_concurrent in the tens.
    limiter = AdaptiveLimiter(initial=args.start_parallel, hard_max=args.parallel)
    governor = StatusGovernor(
        limiter,
        status_url=args.status_url.strip(),
        poll_interval=args.poll_interval,
        idle_fraction=args.idle_fraction,
        pause_seconds=args.pause_seconds,
    )
    print(f"Pacing: up to {args.parallel} request(s) in flight across the whole "
          f"sweep, starting at {args.start_parallel}"
          + (f", governed by {args.status_url.strip()}" if args.status_url.strip()
             else " (governor disabled)"))
    governor.start()

    try:
        # Each query type gets its own thread pool; all pools run concurrently,
        # but the limiter decides how many requests are actually in flight.
        with ThreadPoolExecutor(max_workers=len(query_jobs)) as query_type_executor:
            futures = [
                query_type_executor.submit(
                    run_query_type, name, url_template, ids,
                    args.timeout, args.threads_per_query, counter, counter_lock,
                    total_queries, request_headers, limiter, args.retries,
                    args.backoff_base, args.backoff_cap,
                )
                for name, url_template, ids in query_jobs
            ]
            for future in as_completed(futures):
                future.result()  # re-raise any exceptions
    finally:
        governor.stop()
        limiter.close()

    print("Caching complete.")

if __name__ == "__main__":
    main()
