"""Offline tests for claim_check.py: cite-by-ID, claim support labels, already-addressed, consensus,
the 50-gap cap, and the priority-score hook. No network; the LLM judge is a fake."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import claim_check as cc
import deep_collection as dc
import paper_quality as pq


def _papers():
    return [
        {'title': 'Federated learning for medical imaging privacy', 'year': 2024,
         'abstract': 'We study federated learning across hospitals. Communication cost remains a limitation and no method handles heterogeneous scanners.'},
        {'title': 'Graph neural networks for molecule property prediction', 'year': 2023,
         'abstract': 'Graph neural networks predict molecular properties. Scalability to large molecules is unresolved.'},
        {'title': 'Quantum error correction with surface codes', 'year': 2022, 'abstract': ''},
        {'title': 'Federated optimisation under non-IID medical data', 'year': 2025,
         'abstract': 'We propose a federated optimiser that handles heterogeneous scanners across hospitals and reduces communication cost.'},
    ]


# ─── 1. cite by ID ──────────────────────────────────────────────────
def test_hallucinated_id_cannot_become_evidence():
    papers = _papers()
    id_map = cc.build_id_map(papers)
    gap = {'gap_title': 'x', 'evidence_ids': ['P1', 'P99', 'Paper of Nonsense'], 'evidence_papers': ['Made up title']}
    cc.resolve_evidence_ids(gap, id_map)
    assert gap['evidence_ids'] == ['P1']
    assert 'P99' in gap['evidence_ids_unknown']
    assert gap['evidence_papers'] == [papers[0]['title']]          # model-typed title is replaced


def test_id_formats_are_normalised():
    id_map = cc.build_id_map(_papers())
    gap = {'evidence_ids': ['p2', '[P3]', 4, 'P2']}
    cc.resolve_evidence_ids(gap, id_map)
    assert gap['evidence_ids'] == ['P2', 'P3', 'P4']                # deduplicated, order kept


def test_title_fallback_when_no_ids():
    id_map = cc.build_id_map(_papers())
    gap = {'evidence_papers': ['Some title the model typed']}
    cc.resolve_evidence_ids(gap, id_map)
    assert gap['evidence_papers'] == ['Some title the model typed']  # left for ground_gaps to judge
    assert gap['evidence_ids'] == []


def test_prompt_lines_carry_ids():
    papers = _papers()
    id_map = cc.build_id_map(papers)
    lines = cc.paper_id_lines(papers, id_map).splitlines()
    assert lines[0].startswith('[P1] Federated learning')
    assert lines[3].startswith('[P4] ')


# ─── 2. claim support ───────────────────────────────────────────────
def test_check_claims_labels_and_unverifiable():
    papers = _papers()
    id_map = cc.build_id_map(papers)
    gaps = [
        {'gap_title': 'Heterogeneous scanners in federated imaging', 'description': 'No method handles scanner heterogeneity.',
         'evidence_ids': ['P1']},
        {'gap_title': 'Quantum codes', 'description': 'Surface codes lack decoders for X.', 'evidence_ids': ['P3']},  # no abstract
    ]
    cc.resolve_all(gaps, id_map)
    seen = {}

    def fake_judge(prompt):
        seen['prompt'] = prompt
        return {'results': [{'id': 'g1', 'label': 'supported', 'reason': 'abstract names the limitation'}]}

    summary = cc.check_claims(gaps, papers, fake_judge, pause_s=0)
    assert gaps[0]['claim_check']['label'] == 'supported'
    assert gaps[1]['claim_check']['label'] == 'unverifiable'        # could not check, never "unsupported"
    assert summary['llm_calls'] == 1 and summary['counts']['supported'] == 1
    assert 'Quantum' not in seen['prompt']                          # nothing sent for the abstract-less gap


def test_judge_failure_leaves_unverifiable_not_unsupported():
    papers = _papers()
    gaps = [{'gap_title': 'g', 'description': 'd' * 50, 'evidence_papers': [papers[0]['title']]}]

    def boom(prompt):
        raise RuntimeError('network down')

    cc.check_claims(gaps, papers, boom, pause_s=0, log=lambda *a: None)
    assert gaps[0]['claim_check']['label'] == 'unverifiable'


def test_bad_label_from_judge_is_ignored():
    papers = _papers()
    gaps = [{'gap_title': 'g', 'description': 'd' * 50, 'evidence_papers': [papers[0]['title']]}]
    cc.check_claims(gaps, papers, lambda p: {'results': [{'id': 'g1', 'label': 'definitely'}]}, pause_s=0)
    assert gaps[0]['claim_check']['label'] == 'unverifiable'


def test_only_first_max_gaps_are_judged_and_cap_is_50():
    papers = _papers()
    gaps = [{'gap_title': f'gap number {i} about federated scanners', 'description': 'd' * 50,
             'evidence_papers': [papers[0]['title']]} for i in range(60)]
    calls = []
    cc.check_claims(gaps, papers, lambda p: calls.append(p) or {'results': []}, max_gaps=50, group=5, pause_s=0)
    assert len(calls) == 10                                          # 50 gaps / 5 per call
    assert all('claim_check' in g for g in gaps[:50])
    assert all('claim_check' not in g for g in gaps[50:])


def test_dedupe_cap_applies_to_many_gaps():
    many = [{'gap_title': f'Distinct topic {chr(97 + i % 26)}{i} on subject {i * 7919}', 'severity': 'major',
             'description': f'unique words {i} alpha{i} beta{i * 3} gamma{i * 5}', 'confidence_score': 50}
            for i in range(120)]
    assert len(pq.dedupe_gaps(many, max_gaps=50)) == 50


def test_order_by_support_sinks_contradicted():
    gaps = [{'gap_title': 'a', 'claim_check': {'label': 'contradicted'}},
            {'gap_title': 'b', 'claim_check': {'label': 'supported'}},
            {'gap_title': 'c', 'claim_check': {'label': 'unsupported'}},
            {'gap_title': 'd', 'claim_check': {'label': 'supported'}}]
    assert [g['gap_title'] for g in cc.order_by_support(gaps)] == ['b', 'd', 'c', 'a']


# ─── 3. already addressed ───────────────────────────────────────────
def test_semantic_overlap_flags_recent_similar_papers(monkeypatch):
    import semantic_similarity
    monkeypatch.setattr(semantic_similarity, 'embed_batch', lambda texts: None)   # force the TF-IDF path
    papers = _papers() + [
        {'title': 'Federated learning heterogeneous scanners hospitals communication cost', 'year': 2025,
         'abstract': 'federated learning heterogeneous scanners hospitals communication cost reduction'}]
    gap = {'gap_title': 'Federated learning heterogeneous scanners hospitals',
           'description': 'federated learning with heterogeneous scanners across hospitals and communication cost',
           'evidence_papers': [papers[0]['title']]}
    method = cc.annotate_semantic_overlap([gap], papers, threshold=0.2, min_hits=2)
    assert method == 'tfidf_fallback'
    block = gap['claim_check']
    assert block['semantic_addressed'] is True
    assert papers[0]['title'] not in [a['title'] for a in block['addressed_by']]   # own evidence excluded


def test_semantic_overlap_quiet_for_unrelated_gap(monkeypatch):
    import semantic_similarity
    monkeypatch.setattr(semantic_similarity, 'embed_batch', lambda texts: None)
    gap = {'gap_title': 'Blockchain voting latency', 'description': 'ledger consensus voting latency',
           'evidence_papers': []}
    cc.annotate_semantic_overlap([gap], _papers())
    assert gap['claim_check']['semantic_addressed'] is False


# ─── 4. agreement between runs ──────────────────────────────────────
def test_consensus_keeps_recurring_gaps():
    a = [{'gap_title': 'Federated imaging scanner heterogeneity', 'description': 'federated imaging scanner heterogeneity hospitals'},
         {'gap_title': 'One-off oddity', 'description': 'zebra quokka marmalade'}]
    b = [{'gap_title': 'Scanner heterogeneity in federated imaging', 'description': 'heterogeneity scanner federated imaging hospitals'}]
    kept, dropped = cc.apply_consensus(a, [b], min_agree=2, min_keep=1)
    assert [g['gap_title'] for g in kept] == ['Federated imaging scanner heterogeneity']
    assert dropped == 1
    assert kept[0]['agreement'] == {'runs_agreeing': 2, 'runs_total': 2}


def test_consensus_never_empties_the_report():
    a = [{'gap_title': f'unique{i}', 'description': f'word{i} other{i}'} for i in range(12)]
    kept, _ = cc.apply_consensus(a, [[{'gap_title': 'zzz', 'description': 'nothing alike'}]], min_agree=2, min_keep=10)
    assert len(kept) >= 10


# ─── priority hook ──────────────────────────────────────────────────
def test_priority_score_penalises_unsupported_claims():
    papers = _papers()
    base = {'gap_title': 'g', 'confidence_score': 80, 'evidence_papers': [papers[0]['title']]}
    supported = dict(base, claim_check={'label': 'supported'})
    contradicted = dict(base, claim_check={'label': 'contradicted'})
    out = dc.validate_and_prioritize_gaps([supported, contradicted], papers, {})
    by_label = {g['validation']['claim_support']: g['priority_score'] for g in out}
    assert by_label['supported'] > by_label['contradicted']
    assert out[0]['validation']['claim_support'] == 'supported'
