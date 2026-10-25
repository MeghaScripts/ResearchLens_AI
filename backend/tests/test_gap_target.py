"""The gap count must reach GAPS_TARGET (50) whenever enough distinct, evidence-backed gaps exist, count only
GROUNDED gaps toward it, and say honestly why when it falls short."""
import os
import sys

os.environ.setdefault('APP_SECRET_KEY', 'test-secret-for-tests')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _corpus(n=120):
    fields = ['federated learning privacy clients', 'graph neural network traffic forecasting',
              'protein structure prediction folding', 'speech emotion recognition audio',
              'satellite remote sensing segmentation', 'robot manipulation grasp planning']
    return [{'title': f'{fields[i % 6].split()[0].capitalize()} study {i}: {fields[i % 6]} variant {i}',
             'year': 2020 + i % 6, 'abstract': f'{fields[i % 6]} experiment {i} ' * 6} for i in range(n)]


def _install(monkeypatch, make_gaps):
    import app as appmod

    class _G:
        def __init__(self, **k):
            pass

    calls = {'n': 0}

    def fake_batch(client, topic, pdf_excerpt, papers, kw_str, theme='', n_gaps=6, exclude_titles=None, temperature=0.3):
        calls['n'] += 1
        return {'summary': 's', 'existing_coverage': [], 'emerging_trends': [], 'recommended_next_steps': [],
                'gap_density': 'high', 'overall_recommendation': 'r',
                'research_gaps': make_gaps(calls['n'], papers, n_gaps)}

    monkeypatch.setattr(appmod, 'Groq', _G)
    monkeypatch.setattr(appmod, '_analyze_gaps_batch_with_groq', fake_batch)
    monkeypatch.setattr(appmod.time, 'sleep', lambda s: None)
    monkeypatch.setattr(appmod, '_finish_gap_analysis_with_novelty', lambda *a, **k: None)
    monkeypatch.setattr(appmod, '_claim_judge', lambda client: (lambda prompt: {'results': []}))
    return appmod, calls


def _gap(n, j, paper, grounded=True):
    return {'gap_title': f'Open question {n} {j} zq{n}x{j}', 'severity': 'major', 'confidence_score': 80,
            'description': f'Existing work lacks study number {n} {j} qq{n}{j} under deployment, as seen in {paper["title"]}.',
            'evidence_papers': [paper['title'] if grounded else 'A paper that is not in the collected corpus at all']}


def test_reaches_fifty_even_when_many_gaps_are_ungrounded(monkeypatch):
    def make(n, papers, n_gaps):
        return [_gap(n, j, papers[j % len(papers)], grounded=(j % 5 >= 2)) for j in range(n_gaps)]   # 40% ungrounded

    appmod, calls = _install(monkeypatch, make)
    res = appmod.analyze_gaps_with_groq('k', 'federated learning', '', _corpus(), ['federated'])
    assert len(res['research_gaps']) == appmod.GAPS_TARGET == 50
    assert all(g['grounded'] for g in res['research_gaps'])
    assert res['gap_target']['found'] == 50 and res['gap_target']['stopped_because'] == 'target reached'
    assert res['grounding']['rejected'] >= 0


def test_never_exceeds_the_target(monkeypatch):
    appmod, _ = _install(monkeypatch, lambda n, papers, k: [_gap(n, j, papers[j % len(papers)]) for j in range(12)])
    res = appmod.analyze_gaps_with_groq('k', 'federated learning', '', _corpus(), ['federated'])
    assert len(res['research_gaps']) <= appmod.GAPS_TARGET


def test_reports_honestly_when_the_model_runs_dry(monkeypatch):
    def make(n, papers, n_gaps):                         # the same 6 gaps every call, whatever is asked
        return [_gap(0, j, papers[j]) for j in range(6)]

    appmod, calls = _install(monkeypatch, make)
    res = appmod.analyze_gaps_with_groq('k', 'federated learning', '', _corpus(), ['federated'])
    gt = res['gap_target']
    assert 0 < gt['found'] < gt['target'] == 50
    assert 'no new' in gt['stopped_because']
    assert calls['n'] <= len(appmod._plan_gap_batches_capped(_corpus())) + 3       # stopped early, not 6 more rounds


def test_topup_round_limit_is_respected(monkeypatch):
    monkeypatch.setattr('app.GAP_TOPUP_ROUNDS', 1, raising=False)
    counter = {'n': 0}

    def make(n, papers, n_gaps):
        return [_gap(n, 0, papers[n % len(papers)])]      # one new grounded gap per call: slow progress

    appmod, calls = _install(monkeypatch, make)
    monkeypatch.setattr(appmod, 'GAP_TOPUP_ROUNDS', 1)
    res = appmod.analyze_gaps_with_groq('k', 'federated learning', '', _corpus(), ['federated'])
    assert res['gap_target']['topup_rounds'] <= 1
    assert 'limit' in res['gap_target']['stopped_because'] or res['gap_target']['found'] >= 50
