"""
Offline tests for (1) the deep-collection speed work and (2) verification
running for standard analysis. No network, no API keys.

Run:  python3 backend/tests/test_speed_and_modes.py
"""
import os
import sys
import time

os.environ.setdefault('DEEP_DISABLE_SEMANTIC_MODEL', '1')
os.environ.setdefault('APP_SECRET_KEY', 'test-only-secret-not-used-in-production')
# Importing app creates its SQLite DB and report folders; keep them in a temp
# dir so running the tests can never touch a real database.
import tempfile
_tmp = tempfile.mkdtemp()
os.environ['DB_PATH'] = os.path.join(_tmp, 'test.db')
os.environ['STORAGE_BASE'] = _tmp
os.environ.setdefault('FIREBASE_SERVICE_ACCOUNT_JSON', '{}')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# `tests/` is not a package, so sibling test modules are imported by plain name.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import random
import string

import deep_collection as dc
import paper_verification as pv

_WORDS = [''.join(random.Random(i).choices(string.ascii_lowercase, k=6 + i % 4)) for i in range(4000)]


def _title(tag, page_no, i):
    # distinct, realistic-looking titles (near-identical fake titles would
    # exercise the deduper's worst case instead of the pager under test)
    r = random.Random(f'{tag}-{page_no}-{i}')
    return ' '.join(r.choices(_WORDS, k=8))


def _stub_network():
    dc.backfill_citation_counts = lambda papers, batch_size=500, budget=None: 0
    dc.enrich_with_venue_influence = lambda papers: papers
    dc.enrich_with_author_influence = lambda papers: (0, 0)


def _fake_source(tag, delay, pages_available=50):
    """A paginated source: each page takes `delay` seconds, 100 papers/page."""
    def fn(topic, max_results=100, **kw):
        cursor = next(iter(kw.values())) if kw else 0
        # offset-style cursors are multiples of 100; page-style are 1,2,3...
        page_no = cursor // 100 if cursor >= 100 else cursor
        if page_no >= pages_available:
            return []
        time.sleep(delay)
        return [{'title': _title(tag, page_no, i), 'year': '2023',
                 'source': tag, 'abstract': 'x', 'authors': ['A'],
                 'citation_count': i} for i in range(max_results)]
    return fn


def test_sources_run_in_parallel_not_sum():
    _stub_network()
    # Each source needs ~2.3s (3 pages x 0.5s + 2 polite 0.4s pauses), so 6
    # run one after another take >= ~14s; in parallel the stage takes about
    # as long as one source. Assert well under the sequential figure.
    names = ['ArXiv', 'Semantic Scholar', 'OpenAlex', 'Crossref', 'DOAJ', 'ERIC']
    registry = {n: _fake_source(n, 0.5, pages_available=3) for n in names}
    res = dc.run_two_stage_collection('topic', registry, speed='fast')
    took = res['collection_limits']['seconds']['source_collection']
    assert took < 8.0, f'collection stage took {took:.1f}s - sources are not running in parallel'
    assert set(res['stage0_stats']) == set(names)
    assert res['collection_limits']['speed'] == 'fast'


def test_page_cap_bounds_each_source():
    _stub_network()
    registry = {'OpenAlex': _fake_source('OpenAlex', 0.0, pages_available=1000)}
    res = dc.run_two_stage_collection('topic', registry, speed='fast')
    # fast = 5 pages x 100, so at most 500 per source
    assert res['stage0_stats']['OpenAlex'] <= 500, res['stage0_stats']


def test_time_budget_stops_slow_source_and_keeps_partial_results():
    _stub_network()
    saved = dict(dc.DEEP_SPEED_PRESETS['fast'])
    dc.DEEP_SPEED_PRESETS['fast'].update(source_budget_s=1.2, total_budget_s=3, max_pages=1000,
                                         per_source_cap=100000)
    try:
        registry = {
            'OpenAlex': _fake_source('OpenAlex', 0.4, pages_available=1000),   # would run ~forever
            'Crossref': _fake_source('Crossref', 0.0, pages_available=2),      # finishes fast
        }
        t = time.monotonic()
        res = dc.run_two_stage_collection('topic', registry, speed='fast')
        took = time.monotonic() - t
    finally:
        dc.DEEP_SPEED_PRESETS['fast'].clear()
        dc.DEEP_SPEED_PRESETS['fast'].update(saved)
    assert took < 8, f'budget not enforced ({took:.1f}s)'
    lim = res['collection_limits']
    assert 'OpenAlex' in lim['sources_cut_short_by_time'], lim
    assert res['stage0_stats']['OpenAlex'] > 0, 'partial results from a cut-short source must be kept'
    assert res['stage0_stats']['Crossref'] == 200
    assert 'Crossref' not in lim['sources_cut_short_by_time']


def test_hung_source_is_abandoned_at_stage_deadline():
    _stub_network()
    saved = dict(dc.DEEP_SPEED_PRESETS['fast'])
    dc.DEEP_SPEED_PRESETS['fast'].update(source_budget_s=1, total_budget_s=1)
    try:
        def hang(topic, max_results=100, **kw):
            time.sleep(8)       # one request that never returns in time
            return []
        registry = {'CORE': hang, 'Crossref': _fake_source('Crossref', 0.0, 2)}
        t = time.monotonic()
        res = dc.run_two_stage_collection('topic', registry, speed='fast')
        took = time.monotonic() - t
    finally:
        dc.DEEP_SPEED_PRESETS['fast'].clear()
        dc.DEEP_SPEED_PRESETS['fast'].update(saved)
    assert took < 7.5, f'a hung source blocked the run for {took:.1f}s'
    assert 'CORE' in res['collection_limits']['sources_stopped_at_stage_deadline']
    assert res['stage0_stats'].get('Crossref') == 200


def test_exhaustive_preset_keeps_old_unbounded_limits():
    lim = dc.resolve_limits('exhaustive')
    assert lim['per_source_cap'] == dc.PER_SOURCE_CAP and lim['max_pages'] == dc.MAX_PAGES
    assert dc.resolve_limits('nonsense')['speed'] == dc.DEFAULT_SPEED
    assert dc.resolve_limits(None)['speed'] == dc.DEFAULT_SPEED


def test_query_expansion_is_capped_by_preset():
    _stub_network()
    seen = []
    def fn(topic, max_results=100, **kw):
        seen.append(topic)
        return []
    dc.run_two_stage_collection('t', {'OpenAlex': fn}, speed='fast',
                                extra_queries=[f'q{i}' for i in range(10)])
    assert len(set(seen)) <= dc.DEEP_SPEED_PRESETS['fast']['max_queries']


# ─── arXiv casing regression ─────────────────────────────────────────

def test_arxiv_lowercase_label_is_an_authorized_source():
    # The arXiv fetcher labels papers 'arXiv'; the registry says 'ArXiv'.
    assert pv.validate_source('arXiv') is True
    assert pv.validate_source('ArXiv') is True
    assert pv.check_source_authorized({'source': 'arXiv'})['source_authorized'] is True
    # Only the casing was relaxed - non-registry names are still rejected.
    assert pv.validate_source('random-blog.com') is False
    assert pv.validate_source('https://x.com/paper') is False
    assert pv.check_source_authorized({'source': 'My Blog'})['source_authorized'] is False
    assert pv.validate_source('AAAI (via DBLP)') is True


# ─── standard-mode verification helper ───────────────────────────────

def test_standard_verification_drops_only_rejected_and_labels_rest():
    import app as appmod
    import test_verification_smoke as smoke
    smoke._offline_stubs()
    appmod.STANDARD_VERIFY_TIMEOUT_S = 20
    papers = [
        {'title': 'Deep learning for protein folding prediction', 'abstract': 'protein folding deep learning model',
         'year': '2022', 'authors': ['A B'], 'source': 'arXiv', 'arxiv_id': '2201.00001', 'doi': ''},
        {'title': 'Totally unrelated cooking recipes for pasta', 'abstract': 'pasta sauce tomato',
         'year': '2021', 'authors': ['C D'], 'source': 'OpenAlex', 'doi': ''},
        {'title': 'Something from a blog', 'abstract': 'protein folding', 'year': '2022',
         'authors': ['E'], 'source': 'random-blog.com', 'doi': ''},
    ]
    ctx = {'papers': list(papers), 'topic': 'protein folding deep learning'}
    job = appmod._start_standard_verification(ctx)
    assert job is not None
    # the background job must work on copies, never the caller's dicts
    assert 'verification_status' not in papers[0] or job[1][0] is papers[0]
    info, rejected, removed = appmod._finish_standard_verification(job, ctx, 'protein folding', 'test-uid')
    assert info['ran'] is True, info
    titles_kept = [p['title'] for p in ctx['papers']]
    assert 'Something from a blog' not in titles_kept, 'unauthorized source must be removed'
    assert any('protein folding' in t for t in titles_kept), 'arXiv paper must survive the source check'
    assert all('verification_status' in p for p in ctx['papers']), 'kept papers must be labelled'
    assert removed is True and rejected


def test_standard_verification_timeout_continues_unverified():
    import app as appmod
    from concurrent.futures import Future
    f = Future()    # never completes
    papers = [{'title': 'p', 'source': 'OpenAlex'}]
    ctx = {'papers': list(papers)}
    appmod.STANDARD_VERIFY_TIMEOUT_S = 0.6
    info, rejected, removed = appmod._finish_standard_verification(
        (f, papers, [dict(p) for p in papers], time.monotonic()), ctx, 't', 'u')
    assert info['ran'] is False and 'did not finish' in info['error']
    assert ctx['papers'] == papers and not rejected and not removed


if __name__ == '__main__':
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith('test_') and callable(f)]
    failed = 0
    for n, f in tests:
        try:
            f()
            print(f'  PASS  {n}')
        except Exception as e:
            failed += 1
            import traceback
            print(f'  FAIL  {n}: {e!r}')
            traceback.print_exc()
    print(f'\n{len(tests) - failed}/{len(tests)} passed')
    sys.exit(1 if failed else 0)