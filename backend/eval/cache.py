"""Disk cache for registry lookups, so the evaluation can be re-run (and the thresholds re-tuned) without
re-querying Crossref / OpenAlex / Semantic Scholar every time.

    install('eval/registry_cache.json')                    # record: miss -> real call -> stored
    install('eval/registry_cache.json', cache_only=True)   # replay: a miss behaves like "registry unreachable"

Keys use only the identifying fields of a paper (title, doi, year, authors, ids), never the scores that
verify_papers adds while it runs, so a re-run hits the same entries.
"""
import copy
import json
import os

import paper_verification as pv
import venue_registry as vr
import author_verification as av

_PAPER_FIELDS = ('title', 'doi', 'year', 'authors', 'arxiv_id', 'pmid', 'dblp_key', 'openalex_id')

_TARGETS = [
    (pv, 'crossref_lookup', 'single'),
    (pv, 'crossref_bibliographic_search', 'single'),
    (pv, 'openalex_bibliographic_search', 'single'),
    (pv, 'pubmed_lookup', 'single'),
    (pv, 'openalex_id_lookup', 'single'),
    (pv, 'semantic_scholar_id_lookup', 'single'),
    (pv, 'core_id_lookup', 'single'),
    (pv, 'inspire_id_lookup', 'single'),
    (pv, 'isbn_resolves', 'single'),
    (pv, 'dblp_resolves', 'single'),
    (pv, 'openalex_lookup_batch', 'batch'),
    (pv, 'semantic_scholar_lookup_batch', 'batch'),
    (vr, 'openalex_sources_by_issn', 'batch'),
    (vr, 'openalex_source_by_name', 'single'),
    (av, 'orcid_lookup', 'single'),
]


def _norm(x):
    if isinstance(x, dict):
        if 'title' in x or 'doi' in x:
            return {k: _norm(x.get(k)) for k in _PAPER_FIELDS if x.get(k) is not None}
        return {str(k): _norm(v) for k, v in sorted(x.items(), key=lambda kv: str(kv[0]))}
    if isinstance(x, (list, tuple)):
        return [_norm(i) for i in x]
    if isinstance(x, (set, frozenset)):
        return sorted(_norm(i) for i in x)
    return x


def _encode(o):
    if isinstance(o, (set, frozenset)):
        return {'__set__': sorted(o, key=str)}
    raise TypeError(f'cannot cache {type(o).__name__}')


def _decode(d):
    return set(d['__set__']) if '__set__' in d else d


def install(path, cache_only=False, drop_prefixes=()):
    """Wrap every registry call with the cache. Returns a handle with .save(), .restore(), .stats."""
    store = {}
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            store = json.load(f, object_hook=_decode)
    for key in [k for k in store if any(k.startswith(p + '|') for p in drop_prefixes)]:
        del store[key]
    stats = {'hits': 0, 'misses': 0, 'stored': 0}
    originals = []

    def wrap(module, name, kind):
        original = getattr(module, name, None)
        if original is None:
            return
        originals.append((module, name, original))

        def cached(*args, **kwargs):
            key = name + '|' + json.dumps(_norm([list(args), kwargs]), sort_keys=True, default=str)
            if key in store:
                stats['hits'] += 1
                # a copy: verify_papers pops '__answered__' off the Semantic Scholar batch result, which would
                # otherwise corrupt the stored entry and make replays differ from the first run
                return copy.deepcopy(store[key])
            stats['misses'] += 1
            if cache_only:
                return {} if kind == 'batch' else {'found': False, 'error': 'cache miss (replay mode)'}
            result = original(*args, **kwargs)
            try:
                json.dumps(result, default=_encode)
            except TypeError:
                return result                       # uncacheable result: pass through, don't store
            if name == 'semantic_scholar_lookup_batch' and isinstance(result, dict) and not result.get('__answered__'):
                return result                       # rate-limited / failed batch: never cache a non-answer
            store[key] = copy.deepcopy(result)
            stats['stored'] += 1
            return result

        setattr(module, name, cached)

    for module, name, kind in _TARGETS:
        wrap(module, name, kind)

    class Handle:
        def __init__(self):
            self.stats = stats

        def save(self):
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(store, f, default=_encode)

        def restore(self):
            for module, name, original in originals:
                setattr(module, name, original)

    return Handle()
