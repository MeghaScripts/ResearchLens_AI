"""Tolerant LLM-JSON parsing + retry. Run: python -m pytest tests/test_groq_json.py -q"""
import json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('APP_SECRET_KEY', 'test-only-secret')
os.environ.setdefault('DB_PATH', os.path.join(tempfile.mkdtemp(), 't.db'))
import pytest
import groq_json as gj


@pytest.mark.parametrize('raw,expect', [
    ('{"a": 1}', {'a': 1}),
    ('```json\n{"a": 1}\n```', {'a': 1}),
    ('Here is the JSON:\n{"a": [1, 2,],}', {'a': [1, 2]}),                       # trailing commas + chatter
    ("{a: 1, b: 'x'}".replace("'x'", '"x"'), {'a': 1, 'b': 'x'}),               # unquoted keys
    ('{"t": "line one\nline two"}', {'t': 'line one\nline two'}),               # raw newline in string
    ('{"t": "bad \\q escape"}', {'t': 'bad q escape'}),                          # invalid escape
    ('{"ok": True, "n": None}', {'ok': True, 'n': None}),
])
def test_repairs(raw, expect):
    assert gj.parse_llm_json(raw) == expect


def test_truncated_output_is_salvaged_to_last_complete_item():
    raw = '{"funding_fit": [{"body": "NSF", "why": "fits"}, {"body": "DST", "why": "also fits"}, {"body": "ER", "why": "unterminated str'
    out = gj.parse_llm_json(raw)
    assert [x['body'] for x in out['funding_fit']] == ['NSF', 'DST']


def test_unrecoverable_raises_jsondecodeerror():
    with pytest.raises(json.JSONDecodeError):
        gj.parse_llm_json('I cannot help with that.')


class _Resp:
    def __init__(self, content, finish='stop'):
        self.choices = [type('C', (), {'message': type('M', (), {'content': content})(), 'finish_reason': finish})()]


class _Client:
    def __init__(self, outputs):
        self.outputs, self.calls = list(outputs), []
        self.chat = type('Ch', (), {'completions': self})()
    def create(self, **kw):
        self.calls.append(kw)
        return _Resp(self.outputs.pop(0))


def test_chat_json_retries_with_bigger_budget_then_succeeds():
    c = _Client(['totally not json', '{"literature_review": {"introduction": "ok"}}'])
    out = gj.chat_json(c, 'm', 'p', max_tokens=1500, label='t')
    assert out['literature_review']['introduction'] == 'ok'
    assert c.calls[1]['max_tokens'] > c.calls[0]['max_tokens']
    assert 'valid JSON' in c.calls[1]['messages'][-1]['content']


def test_chat_json_falls_back_when_json_mode_unsupported():
    class C2(_Client):
        def create(self, **kw):
            if 'response_format' in kw:
                raise RuntimeError('response_format json not supported for this model')
            return super().create(**kw)
    out = gj.chat_json(C2(['{"a": 1}']), 'm', 'p')
    assert out == {'a': 1}


def test_chat_json_raises_after_all_attempts():
    with pytest.raises(json.JSONDecodeError):
        gj.chat_json(_Client(['x', 'y', 'z']), 'm', 'p', retries=2)


def test_endpoints_return_200_when_model_output_is_messy(monkeypatch):
    import app as appmod, auth
    auth._init_firebase = lambda: None
    auth.firebase_auth.verify_id_token = lambda tok, **kw: {'uid': 'u1', 'email': 'a@b.c'}
    messy = ['{"literature_review": {"introduction": "i", "existing_work": "e", "gap_analysis": "g", "proposed_direction": "p"},'
             ' "comparison_table": [{"paper": "A", "method": "m", "covers_gap": false, "limitation": "l"},]}',
             '{"funding_fit": [{"body": "NSF", "program_area": "CISE", "why_relevant": "fit", "search_url": "https://www.nsf.gov/funding"}], "search_keywords": ["ai"], "disclaimer": "verify"']
    client = _Client(messy)
    monkeypatch.setattr(appmod, 'Groq', lambda **k: client)
    c = appmod.app.test_client()
    h = {'Authorization': 'Bearer x'}
    r1 = c.post('/api/gap-litreview', json={'api_key': 'k', 'gap_title': 'g'}, headers=h)
    r2 = c.post('/api/gap-funding', json={'api_key': 'k', 'gap_title': 'g'}, headers=h)
    assert r1.status_code == 200 and r1.json['comparison_table'][0]['paper'] == 'A'
    assert r2.status_code == 200 and r2.json['funding_fit'][0]['body'] == 'NSF'
