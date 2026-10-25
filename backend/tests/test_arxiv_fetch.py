"""arXiv fetch must retry and fail over instead of dying on one slow attempt."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

ATOM = ('<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">'
        '<entry><id>http://arxiv.org/abs/2401.00001v1</id><title>A paper</title><summary>An abstract.</summary>'
        '<published>2024-01-01T00:00:00Z</published><author><name>A B</name></author></entry></feed>')


class _Resp:
    def __init__(self, text='', status=200):
        self.text, self.status_code = text, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f'HTTP {self.status_code}')


def test_retries_then_succeeds(monkeypatch):
    import app as appmod
    import requests
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        if len(calls) < 3:
            raise requests.exceptions.ReadTimeout('read timed out')
        return _Resp(ATOM)

    monkeypatch.setattr(appmod.requests, 'get', fake_get)
    monkeypatch.setattr(appmod.time, 'sleep', lambda s: None)
    papers = appmod._fetch_arxiv_for_multi('federated learning', 5)
    assert len(calls) == 3 and calls[-1].startswith('http://')         # third try is the plain-http fallback
    assert papers and papers[0]['source'] == 'arXiv'


def test_raises_only_after_every_try_failed(monkeypatch):
    import app as appmod
    import requests
    monkeypatch.setattr(appmod.requests, 'get', lambda url, **kw: (_ for _ in ()).throw(requests.exceptions.ReadTimeout('x')))
    monkeypatch.setattr(appmod.time, 'sleep', lambda s: None)
    with pytest.raises(requests.exceptions.ReadTimeout):
        appmod._fetch_arxiv_for_multi('federated learning', 5)


def test_timeout_is_configurable(monkeypatch):
    import app as appmod
    seen = {}
    monkeypatch.setenv('ARXIV_TIMEOUT', '33')
    monkeypatch.setattr(appmod.requests, 'get', lambda url, **kw: seen.update(kw) or _Resp(ATOM))
    appmod._fetch_arxiv_for_multi('x', 5)
    assert seen['timeout'] == 33.0
