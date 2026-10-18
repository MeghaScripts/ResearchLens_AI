"""Quality gate, gap de-duplication and theme batching.
Run: cd backend && python3 -m pytest tests/test_quality_and_gaps.py -q"""
import json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('APP_SECRET_KEY', 'test-only-secret')
os.environ.setdefault('FIREBASE_SERVICE_ACCOUNT_JSON', '{}')
os.environ.setdefault('DB_PATH', os.path.join(tempfile.mkdtemp(), 't.db'))
import pytest
import paper_quality as pq

JUNK_FROM_SCREENSHOT = ['jhdjfj', 'fgbfdgf', 'dg', 'DSFSDF', 'dfgsdf', 'sdfdsfd', 'DFGHN', 'FH', 'sdfsdf', 'gfhg', 'dgfdg', 'xcfgdf', 'bgh', 'C', 'sdfgd', 'DGVFD', 'SDFSA', 'ER', 'SDF', 'dafdfafd',
                        'Acknowledgment to the Reviewers of AI in 2022']
REAL = ['Attention Is All You Need', 'BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding',
        'LSTM Networks for Traffic Forecasting', 'Cover Trees for Nearest Neighbor Search', 'Index Selection for Relational Databases',
        'Contents-based Image Retrieval using Convolutional Networks', 'Strengths and Weaknesses of Agentic AI Benchmarks',
        'Mamba: Linear-Time Sequence Modeling with Selective State Spaces', 'Rhythm Analysis in Music Generation Models']


@pytest.mark.parametrize('t', JUNK_FROM_SCREENSHOT)
def test_screenshot_junk_is_rejected(t):
    assert pq.junk_reason({'title': t, 'year': 2026, 'source': 'x'}) is not None


@pytest.mark.parametrize('t', REAL)
def test_real_titles_survive(t):
    assert pq.junk_reason({'title': t, 'year': 2020, 'authors': ['A B'], 'abstract': 'x' * 100}) is None


def test_short_admin_words_only_when_title_is_just_that():
    assert pq.junk_reason({'title': 'Editorial', 'year': 2020}) is not None
    assert pq.junk_reason({'title': 'Author Index', 'year': 2020}) is not None


def test_topic_overlap_and_metadata_and_duplicates():
    papers = [
        {'title': 'Evaluating agentic AI systems in healthcare', 'abstract': 'We study agents. ' * 10, 'year': 2025},
        {'title': 'Soil microbial diversity in alpine meadows', 'abstract': 'We sample soil. ' * 10, 'year': 2024},
        {'title': 'Evaluating agentic AI systems in healthcare', 'abstract': 'dup ' * 30, 'year': 2025},
        {'title': 'Agentic benchmark protocols for clinical tools'},                      # no metadata at all
    ]
    kept, dropped = pq.local_quality_filter(papers, 'agentic AI evaluation')
    assert [p['title'] for p in kept] == ['Evaluating agentic AI systems in healthcare']
    reasons = ' | '.join(d['reason'] for d in dropped)
    assert 'topic' in reasons and 'duplicate' in reasons and 'metadata' in reasons


def test_gate_never_empties_a_run():
    papers = [{'title': 'qwe', 'year': 2020}, {'title': 'zxc', 'year': 2020}]
    kept, dropped = pq.local_quality_filter(papers, 'anything')
    assert len(kept) == 2 and dropped == []


def test_dedupe_keeps_distinct_gaps_beyond_fifteen_and_removes_near_duplicates():
    themes = ['federated learning privacy', 'graph neural traffic', 'protein folding structure', 'quantum error correction',
              'satellite image segmentation', 'speech emotion recognition', 'robot grasp planning', 'legal text summarisation',
              'battery degradation forecasting', 'genomic variant calling', 'wireless spectrum sensing', 'crop disease detection',
              'code vulnerability detection', 'medical report generation', 'music source separation', 'fraud detection payments',
              'drone swarm coordination', 'climate downscaling models']
    gaps = [{'gap_title': f'Lack of {t} benchmarks', 'description': f'Existing work on {t} ignores real deployments of {t}.',
             'severity': 'major', 'confidence_score': 70 + i} for i, t in enumerate(themes)]
    dup = dict(gaps[0], gap_title='No benchmarks for federated learning privacy exist', confidence_score=10)
    out = pq.dedupe_gaps(gaps + [dup])
    assert len(out) == 18                                   # old code capped this at 15
    assert not any(g['confidence_score'] == 10 for g in out)
    assert pq.dedupe_gaps(gaps, max_gaps=5).__len__() == 5


def test_dedupe_ranks_by_severity_then_confidence():
    g = [{'gap_title': 'Alpha topic missing', 'severity': 'moderate', 'confidence_score': 99, 'description': 'alpha'},
         {'gap_title': 'Beta thing absent', 'severity': 'critical', 'confidence_score': 50, 'description': 'beta'},
         {'gap_title': 'Gamma issue open', 'severity': 'critical', 'confidence_score': 80, 'description': 'gamma'}]
    assert [x['gap_title'] for x in pq.dedupe_gaps(g)] == ['Gamma issue open', 'Beta thing absent', 'Alpha topic missing']


def _corpus(n):
    fields = [('federated learning privacy clients', 'FedAvg'), ('graph neural network traffic forecasting', 'GNN'),
              ('protein structure prediction folding', 'Fold'), ('speech emotion recognition audio', 'Emo'),
              ('satellite remote sensing segmentation', 'Sat'), ('robot manipulation grasp planning', 'Rob')]
    out = []
    for i in range(n):
        f, tag = fields[i % len(fields)]
        out.append({'title': f'{tag} study {i}: {f} variant {i}', 'year': 2020 + i % 6, 'abstract': f'{f} experiment {i} ' * 6})
    return out


def test_plan_batches_small_corpus_is_single_batch_and_large_is_themed():
    small = _corpus(30)
    assert len(pq.plan_gap_batches(small)) == 1
    big = _corpus(500)
    plan = pq.plan_gap_batches(big)
    assert 5 <= len(plan) <= 30 and pq.GAP_MAX_BATCHES == 0
    assert sum(len(b) for b, _ in plan) == 500              # every paper analysed exactly once
    assert all(theme for _, theme in plan)


class _FakeGroq:
    calls = 0
    def __init__(self, **k): pass
    @property
    def chat(self): return self
    @property
    def completions(self): return self
    def create(self, **kw):
        _FakeGroq.calls += 1
        n = _FakeGroq.calls
        prompt = kw['messages'][0]['content']
        theme = prompt.split('THEME OF THIS BATCH')[1].split('\n')[0] if 'THEME OF THIS BATCH' in prompt else 'general'
        listed = [l[2:].rsplit(' (', 1)[0] for l in prompt.split('\n') if l.startswith('- ')]
        # each gap cites ONE different paper and talks about it (a real gap does; grounding
        # checks that), so gaps stay distinct and are not merged by the de-duplicator
        gaps = [{'gap_title': f'Batch {n} gap {j} about {theme[:20]} zq{n}x{j}', 'severity': 'major',
                 'description': (f'Existing work on {theme} lacks study number {n} {j} qq{n}{j} '
                                 f'under real deployment conditions, as seen in {listed[j % len(listed)]}.'),
                 'confidence_score': 80, 'evidence_papers': [listed[j % len(listed)]]} for j in range(5)]
        msg = type('M', (), {'content': json.dumps({'summary': 's', 'research_gaps': gaps, 'existing_coverage': [], 'emerging_trends': [],
                                                    'recommended_next_steps': [], 'gap_density': 'high', 'overall_recommendation': 'r'})})()
        return type('R', (), {'choices': [type('C', (), {'message': msg})()]})()


def test_500_papers_now_yield_far_more_than_ten_gaps(monkeypatch):
    import app as appmod
    monkeypatch.setattr(appmod, 'Groq', _FakeGroq)
    monkeypatch.setattr(appmod.time, 'sleep', lambda s: None)
    monkeypatch.setattr(appmod, '_finish_gap_analysis_with_novelty', lambda *a, **k: None)
    _FakeGroq.calls = 0
    res = appmod.analyze_gaps_with_groq('k', 'federated learning', '', _corpus(500), ['federated'])
    assert _FakeGroq.calls >= 5
    assert len(res['research_gaps']) >= 25


def test_local_gate_runs_in_standard_verification_and_is_reported(monkeypatch):
    import app as appmod
    ctx = {'topic': 'federated learning privacy', 'papers': [dict(p, title=p['title'].replace('study', 'federated study')) for p in _corpus(12)[::6]] * 0 + [{'title': f'Federated learning privacy study {i}', 'year': 2024, 'abstract': 'federated privacy ' * 8} for i in range(12)] + [{'title': 'dfgsdf', 'year': 2026},
           {'title': 'Acknowledgment to the Reviewers of AI in 2022', 'year': 2023}]}
    monkeypatch.setattr(appmod, 'STANDARD_VERIFY_ENABLED', False)
    job = appmod._start_standard_verification(ctx)
    assert job is None and len(ctx['papers']) == 12
    info, rejected, removed = appmod._finish_standard_verification(job, ctx, 'federated learning privacy', 'u')
    assert info['local_quality_filter']['dropped'] == 2
    assert {r['verification_status'] for r in rejected} == {'LOW_QUALITY'}


def test_timeout_scales_with_corpus(monkeypatch):
    import app as appmod
    monkeypatch.setattr(appmod, '_STD_TIMEOUT_ENV', None)
    assert appmod._std_verify_timeout(40) < appmod._std_verify_timeout(500) <= 600


def test_junk_with_an_abstract_and_year_is_still_rejected():
    for t in ['sdfgd', 'dafdfafd', 'DGVFD']:
        assert pq.junk_reason({'title': t, 'year': 2026, 'source': 'Crossref', 'abstract': 'lorem ipsum ' * 10}, pq.topic_terms('AI')) is not None


def test_gate_refreshes_live_job_progress(monkeypatch):
    import app as appmod
    calls = []
    monkeypatch.setattr(appmod.jobs, 'set_papers', lambda j, p, *a, **k: calls.append(('papers', len(p))))
    monkeypatch.setattr(appmod.jobs, 'add_step', lambda j, a, s, *x, **k: calls.append(('step', s)))
    ctx = {'_job_id': 'j1', 'topic': 'AI', 'papers': [{'title': 'Deep AI methods for vision', 'year': 2024, 'abstract': 'AI ' * 30},
                                                      {'title': 'sdfgd', 'year': 2026}]}
    appmod._apply_local_quality_gate(ctx)
    assert ('papers', 1) in calls and any(c[0] == 'step' and 'removed 1' in c[1] for c in calls)


def test_default_has_no_gap_cap_and_no_batch_cap():
    assert pq.MAX_GAPS == 0 and pq.GAP_MAX_BATCHES == 0
    gaps = [{'gap_title': f'Distinct topic number {i} {chr(97 + i % 26) * 5}', 'description': f'unique description {i} ' + 'word%d ' % i * 5,
             'severity': 'major', 'confidence_score': 50} for i in range(120)]
    assert len(pq.dedupe_gaps(gaps)) >= 100


PAPERS = [{'title': 'Federated learning with differential privacy for hospitals', 'abstract': 'We train models across hospitals with privacy.',
           'url': 'https://x/1', 'year': 2023},
          {'title': 'Communication-efficient federated optimisation', 'abstract': 'Reducing communication cost.', 'doi': '10.1/abc', 'year': 2022}]


def test_grounding_keeps_real_evidence_and_normalises_titles():
    g = [{'gap_title': 'Privacy in hospital federated training', 'description': 'Hospitals lack evaluated privacy budgets for federated models in practice.',
          'evidence_papers': ['federated learning with differential privacy for hospitals.']}]
    kept, rej = pq.ground_gaps(g, PAPERS)
    assert len(kept) == 1 and not rej
    assert kept[0]['evidence_papers'] == ['Federated learning with differential privacy for hospitals'] and kept[0]['grounded'] is True
    assert kept[0]['evidence_links'] == ['https://x/1']


def test_grounding_rejects_invented_unrelated_and_empty_gaps():
    g = [{'gap_title': 'Made up', 'description': 'A long enough invented description about something nobody studied at all.',
          'evidence_papers': ['A paper that does not exist anywhere in the corpus']},
         {'gap_title': 'No evidence given', 'description': 'A long enough description about federated optimisation and communication.', 'evidence_papers': []},
         {'gap_title': 'Unrelated', 'description': 'Quantum chromodynamics lattice simulations remain unexplored for baryons.',
          'evidence_papers': ['Communication-efficient federated optimisation']},
         {'gap_title': 'Too short', 'description': 'tiny', 'evidence_papers': ['Communication-efficient federated optimisation']}]
    kept, rej = pq.ground_gaps(g, PAPERS)
    assert kept == []
    assert {r['reason'] for r in rej} == {'cites no paper that exists in the collected set', 'evidence papers are unrelated to the gap', 'no real description'}


def test_analysis_result_reports_grounding(monkeypatch):
    import app as appmod
    monkeypatch.setattr(appmod, 'Groq', _FakeGroq)
    monkeypatch.setattr(appmod.time, 'sleep', lambda s: None)
    monkeypatch.setattr(appmod, '_finish_gap_analysis_with_novelty', lambda *a, **k: None)
    res = appmod.analyze_gaps_with_groq('k', 'federated learning', '', _corpus(60), ['federated'])
    assert res['grounding']['kept'] == len(res['research_gaps']) and all(g['grounded'] for g in res['research_gaps'])