"""Offline tests for the per-level evaluation (eval/run_levels.py), fitting helpers, weight overrides,
the optional cross-encoder and the Semantic Scholar key/backoff."""
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paper_verification as pv
import semantic_similarity as ss
from eval import fit, build_eval_set as bes, run_eval, relevance_eval, quality_eval, run_levels, cache as registry_cache
from tests.test_eval_smoke import _stub_registries


def _topic_papers(n_per_topic=14):
    topics = {'federated learning': ('federated', 'clients', 'privacy', 'aggregation'),
              'protein folding': ('protein', 'folding', 'structure', 'residues'),
              'speech emotion': ('speech', 'emotion', 'audio', 'prosody')}
    out, i = [], 0
    for topic, words in topics.items():
        for j in range(n_per_topic):
            w = ' '.join(words)
            out.append({'title': f'{words[0].capitalize()} {words[1]} study {i}: {words[2]} and {words[3]}',
                        'authors': [f'Author{i} A', f'Author{i} B'], 'year': 2020 + j % 5, 'doi': f'10.1234/p{i}',
                        'venue': 'Journal of Tests', 'abstract': f'We investigate {w} with experiments {i}. ' * 3,
                        'citation_count': i, 'source': 'OpenAlex', 'topic': topic})
            i += 1
    return out


def _dataset(tmp_path):
    real = _topic_papers()
    recs = bes.make_dataset(real, [dict(p, title='Retracted ' + p['title'], doi=f'10.9/r{i}') for i, p in enumerate(real[:6])],
                            n_bad=30, seed=3)
    path = str(tmp_path / 'labelled.jsonl')
    bes.save_jsonl(recs, path)
    return recs, path


# ─── fitting helpers ────────────────────────────────────────────────
def test_fit_helpers_basic_properties():
    assert fit.auc([0.9, 0.8, 0.3, 0.2], [1, 1, 0, 0]) == 1.0
    assert fit.auc([0.5, 0.5, 0.5, 0.5], [1, 0, 1, 0]) == 0.5
    assert fit.spearman([1, 2, 3, 4, 5], [10, 20, 30, 40, 50]) > 0.99
    assert fit.spearman([1, 2, 3, 4, 5], [5, 4, 3, 2, 1]) < -0.99
    assert fit.weighted_kappa([1, 2, 3, 4, 5, 3], [1, 2, 3, 4, 5, 3]) == 1.0
    assert fit.weighted_kappa([1, 1, 5, 5], [5, 5, 1, 1]) < 0
    w, _ = fit.ridge_fit([[i, (i * 7) % 5] for i in range(40)], [3 * i + 0.5 * ((i * 7) % 5) for i in range(40)], lam=0.001)
    assert abs(w[0] - 3) < 0.05 and abs(w[1] - 0.5) < 0.05


def test_logistic_nonneg_never_returns_negative_weights():
    X = [[i / 40, 1 - i / 40] for i in range(40)]
    y = [1 if i > 20 else 0 for i in range(40)]
    w, _ = fit.logistic_fit(X, y, nonneg=True)
    assert min(w) >= 0 and w[0] > w[1]


# ─── level 1 / full command ─────────────────────────────────────────
def test_run_levels_end_to_end_offline(monkeypatch, tmp_path, capsys):
    _stub_registries(monkeypatch)
    recs, path = _dataset(tmp_path)
    out = str(tmp_path / 'levels.json')
    rc = run_levels.main(['--data', path, '--cache', str(tmp_path / 'c.json'), '--out', out, '--cv', '3'])
    assert rc == 0
    report = json.load(open(out))
    l1 = report['level1_authenticity']['pooled']
    assert l1['n'] == len([r for r in recs if r['category'] != 'off_topic'])   # off-topic excluded from level 1
    assert 'level2_relevance' in report
    assert '--make-quality-template' in capsys.readouterr().out            # tells you how to get level 3


def test_cv_gives_every_record_one_out_of_fold_prediction(monkeypatch, tmp_path):
    _stub_registries(monkeypatch)
    recs, _ = _dataset(tmp_path)
    pairs = run_eval.verify_once(recs)
    preds, chosen = run_eval.cv_evaluate(pairs, k=4, tune_thresholds=True, auth_grid=(70, 75), rel_grid=(30, 35))
    assert sorted(p['id'] for p in preds) == sorted(r['id'] for r in recs)      # each record exactly once
    assert len(chosen) == 4


def test_run_eval_cv_flag(monkeypatch, tmp_path):
    _stub_registries(monkeypatch)
    _, path = _dataset(tmp_path)
    out = str(tmp_path / 'r.json')
    run_eval.main(['--data', path, '--cache', str(tmp_path / 'c.json'), '--out', out, '--cv', '3'])
    rep = json.load(open(out))
    assert rep['mode'] == '3-fold CV' and rep['pooled']['n'] == rep['n']


# ─── level 2 relevance ──────────────────────────────────────────────
def test_relevance_separates_on_and_off_topic(tmp_path):
    recs, _ = _dataset(tmp_path)
    real, topics = relevance_eval.real_papers(recs)
    assert len(topics) == 3
    rows = relevance_eval.score_pairs(real, topics)
    assert len(rows) == len(real) * 3 and sum(r['label'] for r in rows) == len(real)
    rep = relevance_eval.evaluate(rows, real, k=3)
    assert rep['shipped']['auc'] > 0.95
    assert rep['fitted_out_of_fold']['auc'] > 0.95
    assert 0 <= rep['recommended']['threshold'] <= 100
    assert 'DEEP_RELEVANCE_WEIGHTS' in relevance_eval.format_report(rep)


def test_rescore_matches_the_app_formula_for_shipped_weights(tmp_path):
    recs, _ = _dataset(tmp_path)
    real, topics = relevance_eval.real_papers(recs)
    rows = relevance_eval.score_pairs(real[:5], topics[:1])
    for r in rows:
        assert abs(relevance_eval.rescore(r, dict(pv.RELEVANCE_WEIGHTS)) - r['score']) < 0.6   # score is rounded to 2 dp per paper


def test_best_threshold_and_metrics():
    scores = [10, 20, 30, 70, 80, 90]
    labels = [0, 0, 0, 1, 1, 1]
    t = relevance_eval.best_threshold(scores, labels)
    m = relevance_eval.metrics_at(scores, labels, t)
    assert m['balanced_accuracy'] == 1.0 and m['confusion'] == {'tp': 3, 'fp': 0, 'tn': 3, 'fn': 0}


# ─── level 3 quality ────────────────────────────────────────────────
def test_quality_template_and_evaluation(monkeypatch, tmp_path):
    _stub_registries(monkeypatch)
    recs, path = _dataset(tmp_path)
    sheet = str(tmp_path / 'sheet.csv')
    n = quality_eval.make_template(recs, 30, sheet)
    assert n == 30
    rows = list(csv.DictReader(open(sheet)))
    assert list(rows[0].keys())[-3:] == ['rater1', 'rater2', 'rater3']
    by_id = {r['id']: r for r in recs}
    for row in rows:                                       # synthetic raters who like highly cited papers
        c = by_id[row['id']]['citation_count']
        base = 1 + min(4, c // 9)
        row['rater1'], row['rater2'], row['rater3'] = base, min(5, base + (c % 2)), max(1, base - (c % 2))
    with open(sheet, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=quality_eval.TEMPLATE_FIELDS)
        w.writeheader()
        w.writerows(rows)
    ratings = quality_eval.load_ratings(sheet)
    assert len(ratings) == 30 and quality_eval.agreement(ratings) > 0.5
    rep = quality_eval.evaluate(ratings, recs, k=3)
    assert rep['n_rated'] == 30 and rep['spearman_shipped'] > 0.2   # plumbing check on synthetic data, not a performance claim
    assert 'DEEP_QUALITY_WEIGHTS' in quality_eval.format_report(rep)
    assert abs(sum(rep['recommended_weights'][c] for c in rep['components_fitted'])
               - sum(pv.QUALITY_WEIGHTS[c] for c in rep['components_fitted'])) < 0.01


def test_quality_eval_refuses_tiny_rating_sets(tmp_path):
    recs, _ = _dataset(tmp_path)
    rep = quality_eval.evaluate([{'id': recs[0]['id'], 'ratings': [3], 'mean': 3.0}], recs)
    assert 'error' in rep


# ─── weight overrides, cross-encoder, Semantic Scholar ──────────────
def test_weight_override_env(monkeypatch):
    saved_r, saved_q = dict(pv.RELEVANCE_WEIGHTS), dict(pv.QUALITY_WEIGHTS)
    try:
        monkeypatch.setenv('DEEP_RELEVANCE_WEIGHTS', json.dumps({'title_similarity': 0.5, 'bogus': 9, 'keyword_overlap': -1}))
        monkeypatch.setenv('DEEP_QUALITY_WEIGHTS', '{not json')
        pv._apply_weight_overrides()
        assert pv.RELEVANCE_WEIGHTS['title_similarity'] == 0.5
        assert 'bogus' not in pv.RELEVANCE_WEIGHTS and pv.RELEVANCE_WEIGHTS['keyword_overlap'] == saved_r['keyword_overlap']
        assert pv.QUALITY_WEIGHTS == saved_q                     # bad JSON ignored, weights unchanged
    finally:
        pv.RELEVANCE_WEIGHTS.clear(); pv.RELEVANCE_WEIGHTS.update(saved_r)
        pv.QUALITY_WEIGHTS.clear(); pv.QUALITY_WEIGHTS.update(saved_q)


def test_cross_encoder_is_optional_and_blends_when_available(monkeypatch):
    papers = [{'title': 'Federated learning privacy', 'abstract': 'federated learning with clients'},
              {'title': 'Protein folding', 'abstract': 'protein structure residues'}]
    pv.compute_relevance_scores(papers, 'federated learning')
    assert 'cross_encoder' not in papers[0]['relevance_components']          # off unless configured
    saved = dict(pv.RELEVANCE_WEIGHTS)
    try:
        monkeypatch.setattr(ss, 'cross_encoder_scores', lambda q, texts: [0.99, 0.01])
        papers = [dict(p) for p in papers]
        pv.compute_relevance_scores(papers, 'federated learning')
        assert 'cross_encoder' in papers[0]['relevance_components']
        assert papers[0]['relevance_score'] > papers[1]['relevance_score']
    finally:
        pv.RELEVANCE_WEIGHTS.clear(); pv.RELEVANCE_WEIGHTS.update(saved)


def test_cross_encoder_failure_returns_none(monkeypatch):
    monkeypatch.setattr(ss, 'CROSS_ENCODER_NAME', 'no-such-model')
    monkeypatch.setattr(ss, '_cross_attempted', False)
    monkeypatch.setattr(ss, '_cross', None)
    assert ss.cross_encoder_scores('q', ['t']) is None
    assert ss.cross_encoder_status()['configured'] is True


def test_semantic_scholar_uses_key_and_backs_off_on_429(monkeypatch):
    calls = []

    class R:
        def __init__(self, status, payload=None):
            self.status_code, self._p, self.headers = status, payload, {'Retry-After': '0'}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f'HTTP {self.status_code}')

        def json(self):
            return self._p

    def fake_post(url, params=None, json=None, timeout=None, headers=None):
        calls.append(headers)
        return R(429) if len(calls) < 3 else R(200, [{'title': 'T', 'paperId': 'x', 'authors': [], 'year': 2022}])

    # a fresh copy of the module: other test files stub pv.semantic_scholar_lookup_batch permanently
    import importlib.util
    spec = importlib.util.spec_from_file_location('pv_fresh_s2', pv.__file__)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    monkeypatch.setenv('S2_API_KEY', 'secret')
    monkeypatch.setattr(fresh.requests, 'post', fake_post)
    monkeypatch.setattr(fresh.time, 'sleep', lambda s: None)
    out = fresh.semantic_scholar_lookup_batch([{'doi': '10.1/abc', 'title': 't'}])
    assert len(calls) == 3 and all(h.get('x-api-key') == 'secret' for h in calls)
    assert out['__answered__']                                          # the retried call counts as answered


def test_cache_never_stores_a_rate_limited_s2_batch_and_returns_copies(tmp_path, monkeypatch):
    seen = []

    def failed(papers):
        seen.append(1)
        return {'__answered__': set()}

    monkeypatch.setattr(pv, 'semantic_scholar_lookup_batch', failed)
    h = registry_cache.install(str(tmp_path / 'c.json'))
    try:
        pv.semantic_scholar_lookup_batch([{'title': 'a'}])
        pv.semantic_scholar_lookup_batch([{'title': 'a'}])
        assert len(seen) == 2                                           # not cached, asked again
    finally:
        h.restore()

    monkeypatch.setattr(pv, 'semantic_scholar_lookup_batch', lambda papers: {'k': {'found': True}, '__answered__': {'k'}})
    h = registry_cache.install(str(tmp_path / 'c2.json'))
    try:
        first = pv.semantic_scholar_lookup_batch([{'title': 'b'}])
        first.pop('__answered__')                                       # what verify_papers does
        again = pv.semantic_scholar_lookup_batch([{'title': 'b'}])
        assert again.get('__answered__') == {'k'}                      # stored entry was not corrupted
    finally:
        h.restore()
