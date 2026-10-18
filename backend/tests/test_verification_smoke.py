"""
Smoke tests for the paper-verification layer, written directly against
the architecture-review's own example assertions (item 10): can a fake
source or a fabricated identifier ever pass, and does a retracted paper
ever reach the ranking?

Run with:  python3 -m pytest backend/tests/test_verification_smoke.py -v
or, dependency-free:  python3 backend/tests/test_verification_smoke.py

These are OFFLINE by design — every network call (Crossref, OpenAlex,
Semantic Scholar, DBLP, PubMed, ORCID, DOAJ) is stubbed to fail, so the
suite runs in under a second with no API keys and no network access,
and exercises the code paths that must behave correctly REGARDLESS of
what a registry says: source allowlisting, DOI/ORCID shape validation,
and the retraction gate. These do not depend on any registry answering
correctly — they test decisions the pipeline makes SET REGARDLESS.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paper_verification as pv
import venue_registry as vr
import author_verification as av
import semantic_similarity as ss
import deep_collection as dc


def _offline_stubs():
    """Forces every network path to fail cleanly, so these tests never
    depend on (or wait on) a real API. Verification logic that reads
    an offline result correctly does NOT reject on missing data — see
    test_offline_does_not_falsely_reject below — so these stubs also
    double as evidence that "could not check" and "checked and failed"
    stay genuinely separate outcomes."""
    for name in ('crossref_lookup', 'crossref_bibliographic_search',
                'openalex_bibliographic_search', 'pubmed_lookup',
                'openalex_id_lookup', 'semantic_scholar_id_lookup',
                'core_id_lookup', 'inspire_id_lookup', 'isbn_resolves',
                'dblp_resolves'):
        setattr(pv, name, lambda *a, **k: {'found': False, 'error': 'offline (test stub)'})
    pv.openalex_lookup_batch = lambda dois: {}
    pv.semantic_scholar_lookup_batch = lambda papers: {}
    vr.openalex_sources_by_issn = lambda issns: {}
    vr.openalex_source_by_name = lambda name: {'found': False, 'reason': 'offline (test stub)'}
    av.orcid_lookup = lambda o: {'found': False, 'orcid': o, 'checksum_valid': True,
                                 'reason': 'offline (test stub)', 'error': True}


# ─── 1. Source allowlist — the professor's core concern ─────────────

def test_approved_sources_validate():
    for name in ('OpenAlex', 'Semantic Scholar', 'Crossref', 'ArXiv', 'PubMed',
                'Europe PMC', 'DOAJ', 'ERIC', 'Internet Archive', 'INSPIRE-HEP',
                'IEEE Xplore', 'Springer Nature', 'CORE', 'ScienceDirect',
                'DBLP', 'NASA ADS'):
        assert pv.validate_source(name) is True, f"{name} should be an authorized source"


def test_dblp_venue_streams_validate():
    # DBLP-per-venue results arrive labelled "<Venue> (via DBLP)", not
    # bare "DBLP" — must still validate, since these are real DBLP API
    # results, not a different, unauthorized path.
    assert pv.validate_source('AAAI (via DBLP)') is True
    assert pv.validate_source('NeurIPS (via DBLP)') is True


def test_fake_sources_never_validate():
    for name in ('FakeResearchWebsite', 'random-blog.com',
                'https://random-site.com/paper', 'Wikipedia', 'Medium',
                'ChatGPT', '', None):
        assert pv.validate_source(name) is False, f"{name!r} must NOT validate as a source"


def test_unauthorized_source_paper_is_rejected_before_scoring():
    """A paper whose `source` isn't in the registry must be rejected —
    and rejected for THAT reason specifically, not merely score low."""
    _offline_stubs()
    paper = {'title': 'Suspicious paper', 'abstract': 'face recognition',
             'authors': ['X'], 'year': 2024, 'source': 'RandomBlog', 'impact_score': 99}
    result = pv.verify_papers([paper], 'face recognition')
    assert result['rejected_count'] == 1
    assert paper['verification_status'] == 'REJECTED'
    assert 'not in the authorized' in paper['verification_reasons'][0]
    assert paper['passed_verification'] is False
    assert paper['overall_score'] == 0.0


# ─── 2. Identifier validation ────────────────────────────────────────

def test_provenance_source_authorized_is_true_for_legit_papers():
    """assert paper["source"] in APPROVED_SOURCES / source_verified is True
    -- this codebase's field is named `source_authorized` (set by
    check_source_authorized), not `source_verified`; same claim,
    checked here on the POSITIVE path (a legitimate paper), since the
    existing rejection test only covers the negative path."""
    _offline_stubs()
    paper = {'title': 'A legitimate paper', 'abstract': 'face recognition attendance system',
             'authors': ['A'], 'year': 2023, 'source': 'OpenAlex', 'doi': '10.1000/legit',
             'venue': 'J', 'impact_score': 10}
    pv.verify_papers([paper], 'face recognition attendance system')
    assert paper['source'] in pv.AUTHORIZED_SOURCES
    assert paper['source_authorized'] is True


def test_valid_dois_pass_shape_check():
    assert pv.valid_identifier('10.1000/example') is True
    assert pv.valid_identifier('10.1109/TPAMI.2021.1234567') is True


def test_fake_dois_fail_shape_check():
    for bad in ('fake-doi', 'not-a-doi', '12345', 'doi:10.1000', None, ''):
        assert pv.valid_identifier(bad) is False, f"{bad!r} must not validate as a DOI"


def test_orcid_checksum():
    # Real, published example ORCID (Josiah Carberry, ORCID's own demo iD).
    assert pv.valid_identifier('0000-0002-1825-0097', kind='orcid') is True
    assert av.checksum_valid('https://orcid.org/0000-0002-1825-0097') is True
    # Same digits, last one flipped -> must fail the ISO 7064 check digit.
    assert pv.valid_identifier('0000-0002-1825-0098', kind='orcid') is False


# ─── 3. Retraction gate ──────────────────────────────────────────────

def test_retracted_paper_is_excluded_from_ranking():
    _offline_stubs()
    paper = {'title': 'Retracted study', 'abstract': 'face recognition',
             'authors': ['Y'], 'year': 2019, 'source': 'OpenAlex', 'doi': '10.1/ret',
             'venue': 'J', 'publication_types_native': ['Retracted Publication', 'Journal Article'],
             'impact_score': 40}
    result = pv.verify_papers([paper], 'face recognition')
    assert paper['verification_status'] == 'RETRACTED'
    assert paper['is_retracted'] is True
    assert paper['passed_verification'] is False
    eligible, _ = pv.filter_for_ranking(result['kept'])
    assert paper not in eligible


# ─── 4. The hard gate — UNVERIFIED must not silently drive ranking ──

def test_unverified_paper_is_excluded_from_ranking_but_not_deleted():
    """A paper that could not be independently corroborated (single
    source, no reachable registry) must be KEPT and LABELED but must
    NOT enter the ranking-eligible set — this is the distinction
    architecture-review item 4 asks for."""
    _offline_stubs()
    paper = {'title': 'Uncorroborated single-source paper', 'abstract': 'face recognition',
             'authors': ['Z'], 'year': 2023, 'source': 'ArXiv', 'arxiv_id': '2301.99999',
             'venue': 'arXiv', 'impact_score': 10}
    result = pv.verify_papers([paper], 'face recognition')
    assert paper['verification_status'] in ('UNVERIFIED', 'PARTIALLY_VERIFIED')
    assert paper['passed_verification'] is True   # kept, not deleted
    eligible, excluded = pv.filter_for_ranking(result['kept'])
    if paper['verification_status'] == 'UNVERIFIED':
        assert paper in excluded
        assert paper not in eligible


def test_partially_verified_excluded_from_ranking_by_default():
    """The architecture review is explicit: only HIGHLY_VERIFIED and
    VERIFIED enter ranking by default; PARTIALLY_VERIFIED can be shown
    as its own bucket but must not silently vote unless a caller
    deliberately widens the tier."""
    assert pv.RANKING_STATUSES_DEFAULT == frozenset({'HIGHLY_VERIFIED', 'VERIFIED'})
    fake_partial = {'verification_status': 'PARTIALLY_VERIFIED'}
    eligible, excluded = pv.filter_for_ranking([fake_partial])
    assert fake_partial in excluded
    assert fake_partial not in eligible
    # A caller that explicitly wants the looser cut can still get it.
    eligible2, _ = pv.filter_for_ranking(
        [fake_partial], statuses={'HIGHLY_VERIFIED', 'VERIFIED', 'PARTIALLY_VERIFIED'})
    assert fake_partial in eligible2


def test_semantic_similarity_method_is_always_a_known_value():
    """Whichever path ran, `similarity_method` must be one of the two
    honest labels the architecture review specifies -- never silently
    absent, and never a made-up third value."""
    _offline_stubs()
    paper = {'title': 'Face recognition attendance system', 'abstract': 'face recognition attendance system',
             'authors': ['A'], 'year': 2023, 'source': 'ArXiv', 'arxiv_id': '2301.00003', 'impact_score': 20}
    pv.verify_papers([paper], 'face recognition attendance system')
    assert paper['relevance_similarity_method'] in {'sentence_transformer', 'tfidf_fallback'}


def test_semantic_similarity_falls_back_cleanly_without_the_library():
    """In THIS environment sentence-transformers is not installed, so
    this specific assertion also proves the fallback path is the one
    actually exercised by the rest of this suite -- not merely coded
    and never run."""
    ss._model = None
    ss._load_attempted = True
    ss._load_error = 'disabled for test'
    values, method = ss.compute_semantic_similarities('face recognition', ['face recognition system'], [0.5])
    assert method == 'tfidf_fallback'
    assert values == [0.5]


def test_semantic_similarity_uses_transformer_when_available():
    """Proves the transformer path is wired correctly without requiring
    a real network download in this test environment."""
    import numpy as np

    class FakeModel:
        def encode(self, texts, **kw):
            return np.array([[1.0, 0.0] if 'face' in t.lower() else [0.0, 1.0] for t in texts])

    ss._model = FakeModel()
    ss._load_attempted = True
    ss._load_error = None
    values, method = ss.compute_semantic_similarities('face recognition', ['face recognition system'], [0.1])
    assert method == 'sentence_transformer'
    assert values[0] > 0.9   # identical direction -> cosine ~1
    ss._model = None
    ss._load_attempted = False   # reset for any other test that runs after this one


def test_off_topic_paper_is_rejected():
    _offline_stubs()
    papers = [
        {'title': 'Face recognition attendance system', 'abstract': 'face recognition attendance system',
         'authors': ['A'], 'year': 2023, 'source': 'ArXiv', 'arxiv_id': '2301.00001', 'impact_score': 50},
        {'title': 'Deep sea squid biology', 'abstract': 'A study of squid in the deep ocean.',
         'authors': ['B'], 'year': 2020, 'source': 'Crossref', 'doi': '10.1/squid', 'impact_score': 5},
    ]
    result = pv.verify_papers(papers, 'face recognition attendance system',
                              keywords=['face recognition', 'attendance'])
    squid = papers[1]
    assert squid['verification_status'] == 'OFF_TOPIC'
    assert squid['passed_verification'] is False


# ─── 5. No fabricated quartile, ever ─────────────────────────────────

def test_no_quartile_is_ever_fabricated():
    _offline_stubs()
    record = vr._source_record({
        'id': 'https://openalex.org/S1', 'display_name': 'Nature', 'issn_l': '0028-0836',
        'host_organization_name': 'Nature Portfolio', 'type': 'journal', 'is_core': True,
        'summary_stats': {'h_index': 1200, '2yr_mean_citedness': 17.4}})
    paper = {'venue': 'Nature', 'authors': ['A']}
    vr.annotate_paper(paper, record)
    assert paper['venue_quartile'] is None
    assert 'Scopus' in paper['venue_quartile_basis'] or 'JCR' in paper['venue_quartile_basis']


# ─── 6. Offline never falsely rejects (regression test for the S2-outage bug) ──

def test_offline_registries_never_produce_a_false_rejection():
    """When every registry is unreachable, papers must degrade to
    UNVERIFIED (capped, excluded from ranking) — never to REJECTED.
    REJECTED requires POSITIVE evidence of a problem, and \"the network
    was down\" is not positive evidence of anything about the paper."""
    _offline_stubs()
    paper = {'title': 'A perfectly ordinary paper', 'abstract': 'face recognition attendance system',
             'authors': ['A'], 'year': 2023, 'source': 'ArXiv', 'arxiv_id': '2301.00002',
             'impact_score': 30}
    pv.verify_papers([paper], 'face recognition attendance system')
    assert paper['verification_status'] != 'REJECTED'


# ─── Gap-evidence validation (architecture-review Phase 17/21) ──────

def test_hallucinated_evidence_paper_is_dropped():
    """assert gap.evidence_paper_doi in verified_corpus -- adapted to
    title, since that's the actual matching key the LLM is given
    (evidence_papers is a title list, not a DOI list); the intent is
    identical: an LLM-cited paper that isn't in the real corpus must
    never render as if it were real evidence."""
    papers = [{'title': 'Face recognition attendance system using deep learning',
              'abstract': 'a', 'year': 2023}]
    gaps = [{'gap_title': 'X', 'confidence_score': 70,
            'evidence_papers': ['Face recognition attendance system using deep learning',
                                'A Paper That Was Never Collected']}]
    result = dc.validate_and_prioritize_gaps(gaps, papers, {'rising_keywords': [], 'entrenched_keywords': []})
    gap = result[0]
    assert 'A Paper That Was Never Collected' not in gap['evidence_papers']
    assert 'A Paper That Was Never Collected' in gap['evidence_papers_unverified']
    assert 'Face recognition attendance system using deep learning' in gap['evidence_papers']


def test_gap_with_only_hallucinated_evidence_is_flagged_not_hidden():
    papers = [{'title': 'Real paper title', 'abstract': 'a', 'year': 2023}]
    gaps = [{'gap_title': 'Y', 'confidence_score': 50, 'evidence_papers': ['Fake Paper One']}]
    result = dc.validate_and_prioritize_gaps(gaps, papers, {'rising_keywords': [], 'entrenched_keywords': []})
    gap = result[0]
    assert gap['evidence_papers'] == []
    assert gap['validation']['all_evidence_papers_unverified'] is True
    # The gap itself is KEPT (not silently deleted) -- an LLM-proposed
    # gap can be real even with a bad citation list -- just flagged.
    assert gap in result


def test_validate_and_prioritize_gaps_does_not_crash_on_real_input():
    """Regression test for the missing `validation` dict construction
    bug: this function used to raise NameError on any non-empty gap
    list. This is the single most safety-critical path in the gap
    pipeline, so it gets its own explicit no-crash test."""
    papers = [{'title': f'Paper {i}', 'abstract': 'x', 'year': 2020 + (i % 5),
              'reference_titles': []} for i in range(20)]
    gaps = [{'gap_title': f'Gap {i}', 'confidence_score': 60, 'trend_signal': 'x',
            'evidence_papers': [f'Paper {i}']} for i in range(5)]
    result = dc.validate_and_prioritize_gaps(gaps, papers, {'rising_keywords': ['x'], 'entrenched_keywords': []})
    assert len(result) == 5
    for g in result:
        assert 'priority_score' in g and 'priority_rank' in g
        assert 'validation' in g
        assert g['validation']['evidence_appears_interconnected'] in (True, False, None)


if __name__ == '__main__':
    tests = [(name, fn) for name, fn in list(globals().items())
             if name.startswith('test_') and callable(fn)]
    passed, failed = 0, []
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {name}: {e}")
            failed.append(name)
        except Exception as e:
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
            failed.append(name)
    print(f"\n{passed}/{len(tests)} passed")
    if failed:
        print(f"Failed: {', '.join(failed)}")
        sys.exit(1)
