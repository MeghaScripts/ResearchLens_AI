"""Venue-filtered search must not fail just because Semantic Scholar rate-limits (429).
Run: python -m pytest tests/test_venue_fallback.py -q"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('APP_SECRET_KEY', 'test-only-secret')
os.environ.setdefault('DB_PATH', os.path.join(tempfile.mkdtemp(), 't.db'))
import app as appmod
import academic_sources as acs


def _p(title, venue, src='OpenAlex'):
    return {'title': title, 'abstract': 'computer vision ' * 10, 'year': '2022', 'authors': ['A'], 'source': src, 'venue': venue, 'url': 'http://x/' + title}


def test_cvpr_iccv_eccv_have_dblp_streams():
    for v in ('CVPR', 'ICCV', 'ECCV'):
        assert v in acs.DBLP_VENUE_STREAMS


def test_aliases_match_full_conference_names():
    w = ['CVPR', 'ICCV', 'ECCV']
    assert appmod._venue_matches('2022 IEEE/CVF Conference on Computer Vision and Pattern Recognition (CVPR)', w)
    assert appmod._venue_matches('Proceedings of the IEEE/CVF International Conference on Computer Vision', w)
    assert appmod._venue_matches('Lecture Notes in ... European Conference on Computer Vision', w)
    assert not appmod._venue_matches('Journal of Soil Science', w)


def test_dblp_down_and_s2_429_still_returns_openalex_papers(monkeypatch):
    def dblp(*a, **k): raise RuntimeError('dblp reset')
    def s2(*a, **k): raise RuntimeError('rate-limited (429) by Semantic Scholar')
    pool = [_p('Detecting objects', '2022 IEEE/CVF Conference on Computer Vision and Pattern Recognition (CVPR)'),
            _p('Depth from video', 'Proceedings of the IEEE/CVF International Conference on Computer Vision'),
            _p('Soil study', 'Journal of Soil Science')]
    monkeypatch.setattr(acs, 'fetch_dblp_venue', dblp)
    monkeypatch.setattr(appmod, '_fetch_semantic_scholar', s2)
    monkeypatch.setattr(appmod, '_fetch_openalex', lambda topic, n=50, page=1: pool if page == 1 else [])
    monkeypatch.setattr(appmod.time, 'sleep', lambda s: None)
    out = appmod.fetch_papers_multi_source('Computer Vision', 50, venue_filter=['CVPR', 'ICCV', 'ECCV'])
    assert {p['title'] for p in out} == {'Detecting objects', 'Depth from video'}


def test_still_reports_a_clear_error_when_nothing_works(monkeypatch):
    import pytest
    monkeypatch.setattr(acs, 'fetch_dblp_venue', lambda *a, **k: [])
    monkeypatch.setattr(appmod, '_fetch_semantic_scholar', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('429')))
    monkeypatch.setattr(appmod, '_fetch_openalex', lambda *a, **k: [])
    monkeypatch.setattr(appmod.time, 'sleep', lambda s: None)
    with pytest.raises(Exception) as ei:
        appmod.fetch_papers_multi_source('Computer Vision', 50, venue_filter=['CVPR'])
    assert 'CVPR' in str(ei.value)


def test_semantic_scholar_uses_api_key_when_configured(monkeypatch):
    seen = {}
    class R:
        status_code = 200
        headers = {}
        def raise_for_status(self): pass
        def json(self): return {'data': []}
    monkeypatch.setenv('SEMANTIC_SCHOLAR_API_KEY', 'k123')
    monkeypatch.setattr(appmod.requests, 'get', lambda url, **kw: (seen.update(kw.get('headers', {})), R())[1])
    appmod._fetch_semantic_scholar('x', 5)
    assert seen.get('x-api-key') == 'k123'
