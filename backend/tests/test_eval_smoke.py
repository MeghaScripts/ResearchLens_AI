"""Offline smoke tests for the verification evaluation harness (backend/eval/). No network: every
registry call is stubbed, so CI can run this on every push."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paper_verification as pv
import venue_registry as vr
import author_verification as av
from eval import metrics, build_eval_set as bes, run_eval, cache as registry_cache


def _real(n=12):
    topics = ['federated learning', 'graph neural networks']
    return [{'title': f'A study of {topics[i % 2]} variant number {i}', 'authors': [f'Author{i} A', f'Author{i} B'],
             'year': 2020 + i % 5, 'doi': f'10.1234/real{i}', 'venue': 'Journal of Tests',
             'abstract': f'We study {topics[i % 2]} in setting {i} and report results on benchmark {i}.',
             'citation_count': i, 'source': 'OpenAlex', 'topic': topics[i % 2]} for i in range(n)]


def _retracted(n=4):
    return [dict(p, title='Retracted ' + p['title'], doi=f'10.9999/ret{i}') for i, p in enumerate(_real(n))]


def _stub_registries(monkeypatch):
    for name in ('crossref_lookup', 'crossref_bibliographic_search', 'openalex_bibliographic_search', 'pubmed_lookup',
                 'openalex_id_lookup', 'semantic_scholar_id_lookup', 'core_id_lookup', 'inspire_id_lookup',
                 'isbn_resolves', 'dblp_resolves'):
        monkeypatch.setattr(pv, name, lambda *a, **k: {'found': False, 'error': 'offline (test stub)'})
    monkeypatch.setattr(pv, 'openalex_lookup_batch', lambda dois: {})
    monkeypatch.setattr(pv, 'semantic_scholar_lookup_batch', lambda papers: {})
    monkeypatch.setattr(vr, 'openalex_sources_by_issn', lambda issns: {})
    monkeypatch.setattr(vr, 'openalex_source_by_name', lambda name: {'found': False, 'reason': 'offline'})
    monkeypatch.setattr(av, 'orcid_lookup', lambda o: {'found': False, 'orcid': o, 'checksum_valid': True,
                                                      'reason': 'offline', 'error': True})


# ─── metrics ────────────────────────────────────────────────────────
def test_wilson_interval_is_sane():
    lo, hi = metrics.wilson_interval(95, 100)
    assert 0.88 < lo < 0.95 < hi < 0.99
    assert metrics.wilson_interval(0, 0) == (0.0, 0.0)


def test_summarise_separates_coverage_from_accuracy():
    rows = [('accept', 'accept')] * 8 + [('accept', 'review')] * 2 + [('reject', 'reject')] * 7 + [('reject', 'accept')]
    s = metrics.summarise(rows)
    assert s['n'] == 18 and s['decided'] == 16 and s['review'] == 2
    assert round(s['coverage'], 3) == round(16 / 18, 3)
    assert round(s['decisive_accuracy'], 3) == round(15 / 16, 3)
    assert s['strict_accuracy'] < s['decisive_accuracy']                # abstentions are misses here
    assert s['confusion']['reject']['accept'] == 1
    assert s['per_class']['accept']['precision'] == 8 / 9


def test_decision_band_maps_all_statuses():
    band = lambda s: pv.decision_band({'verification_status': s})
    assert band('HIGHLY_VERIFIED') == band('VERIFIED') == 'accept'
    assert band('REJECTED') == band('RETRACTED') == band('OFF_TOPIC') == 'reject'
    assert band('PARTIALLY_VERIFIED') == band('UNVERIFIED') == band(None) == 'review'


# ─── dataset construction ───────────────────────────────────────────
def test_dataset_is_deterministic_and_labelled():
    a = bes.make_dataset(_real(), _retracted(), n_bad=20, seed=1)
    b = bes.make_dataset(_real(), _retracted(), n_bad=20, seed=1)
    assert a == b
    assert sum(r['label'] == 'accept' for r in a) == 12 and sum(r['label'] == 'reject' for r in a) == 20
    cats = {r['category'] for r in a}
    assert {'real', 'wrong_doi', 'fake_doi', 'altered_title', 'invented', 'off_topic'} <= cats
    assert len({r['id'] for r in a}) == len(a)


def test_wrong_doi_really_swaps_the_doi():
    recs = bes.make_dataset(_real(), _retracted(), n_bad=20, seed=2)
    real_by_title = {r['title']: r for r in recs if r['category'] == 'real'}
    for r in recs:
        if r['category'] == 'wrong_doi':
            assert r['doi'] != real_by_title[r['title']]['doi']


def test_split_is_stratified_and_disjoint():
    recs = bes.make_dataset(_real(), _retracted(), n_bad=20, seed=3)
    train, test = bes.split_train_test(recs, 0.3, seed=3)
    assert not ({r['id'] for r in train} & {r['id'] for r in test})
    assert len(train) + len(test) == len(recs)
    assert {r['category'] for r in test} >= {'real', 'wrong_doi', 'invented'}


def test_abstract_reconstruction():
    assert bes.abstract_from_inverted_index({'hello': [0], 'world': [1], 'again': [2]}) == 'hello world again'


# ─── end to end, offline ────────────────────────────────────────────
def test_evaluate_runs_offline_and_never_crashes(monkeypatch):
    _stub_registries(monkeypatch)
    recs = bes.make_dataset(_real(), _retracted(), n_bad=12, seed=4)
    preds = run_eval.evaluate(recs)
    assert len(preds) == len(recs)
    assert {p['prediction'] for p in preds} <= {'accept', 'reject', 'review'}
    # With every registry offline nothing can be corroborated, so no fabricated paper may be ACCEPTED.
    assert not [p for p in preds if p['label'] == 'reject' and p['prediction'] == 'accept']


def test_evaluate_restores_thresholds(monkeypatch):
    _stub_registries(monkeypatch)
    before = (pv.AUTHENTICITY_REJECT_THRESHOLD, pv.RELEVANCE_DISCARD_THRESHOLD)
    run_eval.evaluate(bes.make_dataset(_real(), _retracted(), n_bad=6, seed=5), auth_min=60, rel_min=20)
    assert (pv.AUTHENTICITY_REJECT_THRESHOLD, pv.RELEVANCE_DISCARD_THRESHOLD) == before


def test_tune_returns_a_grid_point(monkeypatch):
    _stub_registries(monkeypatch)
    train = bes.make_dataset(_real(), _retracted(), n_bad=8, seed=6)
    best, table = run_eval.tune(train, target=0.95, auth_grid=(70, 75), rel_grid=(30, 35))
    assert len(table) == 4 and best['auth_min'] in (70, 75) and best['rel_min'] in (30, 35)


def test_registry_cache_records_then_replays(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(pv, 'crossref_lookup', lambda doi: calls.append(doi) or {'found': True, 'title': 'T'})
    path = str(tmp_path / 'cache.json')
    h = registry_cache.install(path)
    try:
        assert pv.crossref_lookup('10.1/a') == {'found': True, 'title': 'T'}
        assert pv.crossref_lookup('10.1/a') == {'found': True, 'title': 'T'}
        assert calls == ['10.1/a'] and h.stats['hits'] == 1
        h.save()
    finally:
        h.restore()
    calls.clear()
    h2 = registry_cache.install(path, cache_only=True)
    try:
        assert pv.crossref_lookup('10.1/a')['found'] is True               # replayed from disk
        assert pv.crossref_lookup('10.1/other')['found'] is False          # miss in replay mode = unreachable
        assert calls == []
    finally:
        h2.restore()


# ─── fast tuning and the opt-in strict rule ────────────────────────
def test_decide_matches_a_full_rerun(monkeypatch):
    _stub_registries(monkeypatch)
    recs = bes.make_dataset(_real(), _retracted(), n_bad=8, seed=7)
    pairs = run_eval.verify_once(recs)
    fast = run_eval.decide(pairs, auth_min=60, rel_min=20)
    full = run_eval.evaluate(recs, auth_min=60, rel_min=20)
    assert [(p['id'], p['prediction']) for p in fast] == [(p['id'], p['prediction']) for p in full]


def _registry_record(title, authors):
    return {'found': True, 'title': title, 'authors': authors, 'year': 2022, 'venue': 'J'}


def test_severe_mismatch_detection():
    paper = {'title': 'Federated learning for medical imaging privacy across hospitals',
             'authors': ['Alice Nguyen', 'Bob Okafor', 'Chen Wei']}
    same = _registry_record(paper['title'], ['Alice Nguyen', 'Bob Okafor', 'Chen Wei'])
    assert pv._severe_mismatches(paper, [same]) == []
    reordered = _registry_record(paper['title'], ['Wei Chen', 'Nguyen Alice'])        # name order differs
    assert pv._severe_mismatches(paper, [reordered]) == []
    truncated = _registry_record('Federated learning for medical imaging privacy', paper['authors'])
    assert pv._severe_mismatches(paper, [truncated]) == []
    swapped = _registry_record(paper['title'], ['Dmitri Volkov', 'Priya Raman'])
    assert pv._severe_mismatches(paper, [swapped]) == ['authors']
    scrambled = _registry_record('Adaptive hospitals imaging for privacy learning medical across Federated', paper['authors'])
    assert pv._severe_mismatches(paper, [scrambled]) == ['title']
    assert pv._severe_mismatches(paper, []) == []                                      # no registry record -> no claim
    assert pv._severe_mismatches(paper, [{'found': False}]) == []


def test_strict_rule_is_off_by_default_and_rejects_only_when_enabled(monkeypatch):
    paper = {'title': 't', 'authenticity_severe_mismatches': ['authors'], 'authenticity_basis': 'registry_verified',
             'authenticity_score': 80, 'relevance_score': 90, 'quality_score': 50, 'authenticity_contradictions': ['authors_match']}
    monkeypatch.setattr(pv, 'STRICT_FIELD_REJECT', False)
    assert pv.decide_verdict(dict(paper))['verification_status'] == 'PARTIALLY_VERIFIED'
    monkeypatch.setattr(pv, 'STRICT_FIELD_REJECT', True)
    assert pv.decide_verdict(dict(paper))['verification_status'] == 'REJECTED'
    unsure = dict(paper, authenticity_basis='uncorroborated_single_source')            # no registry vouched for the record
    assert pv.decide_verdict(unsure)['verification_status'] != 'REJECTED'
