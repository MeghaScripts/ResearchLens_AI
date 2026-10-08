"""
End-to-end route tests through Flask's test client with the network, the
LLM and Firebase stubbed out. Checks the wiring, not the registries:

  * /api/analyze (standard mode) now runs verification, removes REJECTED
    papers before gap detection, labels the rest, persists a 'standard' run
  * /api/deep-research honours `speed`, reports collection_limits/timings,
    passes the evidence cap to gap detection, persists a 'deep' run
  * /api/deep-research/sources lists sources for the picker
  * /api/verification-runs?mode= filters by run type

Run:  python3 backend/tests/test_routes_e2e.py
"""
import os, sys, tempfile, random, string, io

_tmp = tempfile.mkdtemp()
os.environ.update({
    'DEEP_DISABLE_SEMANTIC_MODEL': '1', 'APP_SECRET_KEY': 'test-only-secret',
    'DB_PATH': os.path.join(_tmp, 't.db'), 'STORAGE_BASE': _tmp,
    'FIREBASE_SERVICE_ACCOUNT_JSON': '{}',
})
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auth
auth._init_firebase = lambda: None
auth.firebase_auth.verify_id_token = lambda tok: {'uid': 'u-test', 'email': 't@example.com'}

import app as appmod
import deep_collection as dc
import tests.test_verification_smoke as smoke
smoke._offline_stubs()
dc.backfill_citation_counts = lambda papers, batch_size=500, budget=None: 0
dc.enrich_with_venue_influence = lambda papers: papers
dc.enrich_with_author_influence = lambda papers: (0, 0)

H = {'Authorization': 'Bearer x'}
client = appmod.app.test_client()
WORDS = [''.join(random.Random(i).choices(string.ascii_lowercase, k=5 + i % 5)) for i in range(2000)]


def _paper(i, source='OpenAlex', topic_words='protein folding'):
    r = random.Random(i)
    return {'title': f"{topic_words} {' '.join(r.choices(WORDS, k=5))}",
            'abstract': f"{topic_words} study {' '.join(r.choices(WORDS, k=20))}",
            'year': str(2018 + i % 7), 'authors': [f'Ann Auth{i}'], 'source': source,
            'doi': '', 'citation_count': i % 50}


def test_standard_analysis_runs_verification_and_filters():
    papers = [_paper(i, 'arXiv' if i % 2 else 'OpenAlex') for i in range(10)]
    papers.append({**_paper(99), 'title': 'protein folding scraped from a blog', 'source': 'random-blog.com'})
    seen = {}

    ag = appmod.orchestrator.agents
    ag['search_agent'].fetch_fn = lambda topic, **kw: [dict(p) for p in papers]

    def fake_gap_execute(context):
        seen['titles_at_gap_time'] = [p['title'] for p in context['papers']]
        context['analysis'] = {'research_gaps': [], 'summary': 's', 'novelty_score': 50}
        return {'analysis': context['analysis']}
    ag['gap_detection_agent'].execute = fake_gap_execute

    resp = client.post('/api/analyze', headers=H, data={'api_key': 'k', 'topic': 'protein folding'})
    assert resp.status_code == 200, resp.get_data(as_text=True)[:500]
    body = resp.get_json()
    v = body['verification']
    assert v['ran'] is True, v
    assert v['persisted'] is True and v.get('run_id')
    assert 'protein folding scraped from a blog' not in seen['titles_at_gap_time'], \
        'rejected paper reached gap detection'
    assert len(seen['titles_at_gap_time']) == 10, 'arXiv-labelled papers must not be rejected'
    assert [p['title'] for p in body['rejected_papers']] == ['protein folding scraped from a blog']
    assert all('verification_status' in p and 'overall_score' in p for p in body['papers'])
    return v['run_id']


def test_standard_run_is_listed_with_mode_filter():
    run_id = test_standard_analysis_runs_verification_and_filters()
    std = client.get('/api/verification-runs?mode=standard', headers=H).get_json()['runs']
    assert any(r['id'] == run_id and r['mode'] == 'standard' for r in std)
    deep = client.get('/api/verification-runs?mode=deep', headers=H).get_json()['runs']
    assert not any(r['id'] == run_id for r in deep)
    papers = client.get(f'/api/verification-runs/{run_id}/papers', headers=H).get_json()
    assert papers, 'persisted run must expose its papers'


def _stub_deep_agents(captured):
    ag = appmod.orchestrator.agents
    sources = {
        'OpenAlex': lambda topic, max_results=100, page=1, **k: (
            [_paper(1000 * page + i) for i in range(max_results)] if page <= 50 else []),
        'Crossref': lambda topic, max_results=100, offset=0, **k: (
            [_paper(500000 + offset + i, 'Crossref') for i in range(max_results)] if offset < 5000 else []),
    }
    def build_registry(ctx):
        captured['requested_sources'] = ctx.get('paper_sources')
        return {k: v for k, v in sources.items()
                if not ctx.get('paper_sources') or k in ctx['paper_sources']}
    ag['deep_collection_agent'].registry_builder_fn = build_registry
    ag['trend_forecasting_agent'].forecast_fn = lambda *a, **k: {'summary': 'f'}

    def fake_gap(api_key, topic, papers, trend_stats, current_trends=None, forecast=None,
                 max_evidence_papers=None):
        captured['max_evidence_papers'] = max_evidence_papers
        captured['gap_papers'] = len(papers)
        return {'research_gaps': [], 'summary': 's'}
    ag['trend_gap_detection_agent'].gap_analysis_fn = fake_gap


def test_deep_research_fast_mode_end_to_end():
    cap = {}
    _stub_deep_agents(cap)
    resp = client.post('/api/deep-research', headers=H, json={
        'api_key': 'k', 'topic': 'protein folding', 'expand_queries': False,
        'drop_unverified': False})
    assert resp.status_code == 200, resp.get_data(as_text=True)[:600]
    b = resp.get_json()
    assert b['speed'] == 'fast'
    lim = b['collection_limits']
    assert lim['per_source_cap'] == 500 and lim['max_pages_per_query'] == 5
    assert all(n <= 500 for n in b['stage0_stats'].values()), b['stage0_stats']
    assert cap['max_evidence_papers'] == 200
    assert set(b['timings']) >= {'collection', 'verification', 'gap_detection', 'total'}
    assert b['verification']['persisted'] is True
    deep_runs = client.get('/api/verification-runs?mode=deep', headers=H).get_json()['runs']
    assert any(r['id'] == b['verification']['run_id'] and r['speed'] == 'fast' for r in deep_runs)


def test_deep_research_source_selection_is_honoured():
    cap = {}
    _stub_deep_agents(cap)
    resp = client.post('/api/deep-research', headers=H, json={
        'api_key': 'k', 'topic': 'protein folding', 'expand_queries': False,
        'drop_unverified': False, 'paper_sources': 'Crossref'})   # comma string accepted
    assert resp.status_code == 200, resp.get_data(as_text=True)[:600]
    b = resp.get_json()
    assert list(b['stage0_stats']) == ['Crossref'], b['stage0_stats']
    assert cap['requested_sources'] == ['Crossref']


def test_deep_research_unknown_speed_falls_back_and_exhaustive_is_flagged():
    cap = {}
    _stub_deep_agents(cap)
    b = client.post('/api/deep-research', headers=H, json={
        'api_key': 'k', 'topic': 'protein folding', 'expand_queries': False,
        'drop_unverified': False, 'speed': 'warp-9'}).get_json()
    assert b['speed'] == 'fast'
    assert b['collection_limits']['is_exhaustive'] is False


def test_sources_endpoint():
    b = client.get('/api/deep-research/sources', headers=H).get_json()
    names = {s['name'] for s in b['sources']}
    assert {'ArXiv', 'Semantic Scholar', 'OpenAlex', 'Crossref', 'PubMed', 'IEEE Xplore'} <= names
    ieee = next(s for s in b['sources'] if s['name'] == 'IEEE Xplore')
    assert ieee['keyed'] is True and ieee['available'] is False     # no key in test env
    assert b['default_speed'] == 'fast' and set(b['speeds']) == {'fast', 'balanced', 'exhaustive'}


if __name__ == '__main__':
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith('test_') and callable(f)]
    failed = 0
    for n, f in tests:
        try:
            f(); print(f'  PASS  {n}')
        except Exception as e:
            failed += 1
            import traceback; print(f'  FAIL  {n}: {e!r}'); traceback.print_exc()
    print(f'\n{len(tests) - failed}/{len(tests)} passed')
    sys.exit(1 if failed else 0)