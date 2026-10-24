"""Fault-injection tests: the pipeline must degrade, not crash, when dependencies misbehave.
Everything is offline; failures are injected by stubbing the registries / the LLM."""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paper_verification as pv
import venue_registry as vr
import author_verification as av
import claim_check as cc
import jobs
import paper_quality as pq


def _papers():
    return [{'title': f'Federated learning study {i}', 'authors': [f'A{i}'], 'year': 2022, 'doi': f'10.1/x{i}',
             'venue': 'J', 'abstract': 'federated learning study of privacy', 'source': 'OpenAlex', 'impact_score': i}
            for i in range(6)]


def _all_registries_raise(monkeypatch, exc):
    def boom(*a, **k):
        raise exc
    for name in ('crossref_lookup', 'crossref_bibliographic_search', 'openalex_bibliographic_search', 'pubmed_lookup',
                 'openalex_id_lookup', 'semantic_scholar_id_lookup', 'core_id_lookup', 'inspire_id_lookup',
                 'isbn_resolves', 'dblp_resolves', 'openalex_lookup_batch', 'semantic_scholar_lookup_batch'):
        monkeypatch.setattr(pv, name, boom)
    monkeypatch.setattr(vr, 'openalex_sources_by_issn', boom)
    monkeypatch.setattr(vr, 'openalex_source_by_name', boom)
    monkeypatch.setattr(av, 'orcid_lookup', boom)


def test_registries_raising_connection_errors_do_not_crash_verification(monkeypatch):
    _all_registries_raise(monkeypatch, ConnectionError('registry down'))
    papers = _papers()
    summary = pv.verify_papers(papers, 'federated learning', network_top_n=0)
    assert summary['verified'] == len(papers)
    assert all(p.get('verification_status') for p in papers)
    # nothing could be corroborated, so nothing may be ACCEPTED on the strength of missing data
    assert not [p for p in papers if pv.decision_band(p) == 'accept']


def test_registries_timing_out_do_not_crash_verification(monkeypatch):
    _all_registries_raise(monkeypatch, TimeoutError('timed out'))
    papers = _papers()
    pv.verify_papers(papers, 'federated learning', network_top_n=0)
    assert all(p.get('verification_status') for p in papers)


def test_malformed_records_do_not_crash_verification(monkeypatch):
    for name in ('crossref_lookup', 'crossref_bibliographic_search', 'openalex_bibliographic_search'):
        monkeypatch.setattr(pv, name, lambda *a, **k: {'found': False, 'error': 'offline'})
    monkeypatch.setattr(pv, 'openalex_lookup_batch', lambda d: {})
    monkeypatch.setattr(pv, 'semantic_scholar_lookup_batch', lambda p: {})
    bad = [{'title': None}, {'title': 'x', 'authors': 'not a list', 'year': 'abc', 'doi': 12345},
           {'title': 'Real looking title about federated learning', 'authors': None, 'year': None, 'abstract': None},
           {}]
    pv.verify_papers(bad, 'federated learning', network_top_n=0)
    assert all('verification_status' in p for p in bad)


def test_claim_check_survives_quota_error_and_keeps_every_gap():
    class GroqQuotaExhausted(Exception):
        pass

    papers = [{'title': f'Paper number {i} on federated learning', 'year': 2024,
               'abstract': 'federated learning abstract text ' * 5} for i in range(10)]
    gaps = [{'gap_title': f'gap {i} about federated learning', 'description': 'd' * 60,
             'evidence_papers': [papers[i]['title']]} for i in range(10)]

    def judge(prompt):
        raise GroqQuotaExhausted('daily budget used')

    out, summary = cc.annotate_gaps(gaps, papers, llm_json_fn=judge, log=lambda *a: None)
    assert len(out) == 10
    assert all(g['claim_check']['label'] == 'unverifiable' for g in out)


def test_claim_check_handles_garbage_judge_output():
    papers = [{'title': 'Paper about federated learning', 'year': 2024, 'abstract': 'federated learning abstract ' * 5}]
    for garbage in (None, 'text', [], {'results': 'nope'}, {'results': [None, 5, {'id': 'g1'}]}):
        gaps = [{'gap_title': 'g', 'description': 'd' * 60, 'evidence_papers': [papers[0]['title']]}]
        cc.check_claims(gaps, papers, lambda p, g=garbage: g, pause_s=0, log=lambda *a: None)
        assert gaps[0]['claim_check']['label'] == 'unverifiable'


def test_dedupe_gaps_tolerates_junk_items():
    junk = [None, 5, 'text', {}, {'gap_title': ''}, {'gap_title': 'ok gap', 'description': None, 'confidence_score': 'high'}]
    out = pq.dedupe_gaps(junk, max_gaps=50)
    assert [g['gap_title'] for g in out] == ['ok gap']


def test_job_store_is_thread_safe_and_isolated_per_user():
    ids, lock = [], threading.Lock()

    def make(uid):
        for _ in range(50):
            jid = jobs.create_job(uid)
            with lock:
                ids.append((jid, uid))

    threads = [threading.Thread(target=make, args=(f'user{i}',)) for i in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(ids) == 800 and len({j for j, _ in ids}) == 800          # no lost or duplicate IDs
    jid, uid = ids[0]
    assert jobs.get_job(jid, uid) is not None
    assert not jobs.get_job(jid, 'someone-else')                          # another user cannot read it
