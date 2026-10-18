"""
Research Chatbot tests (isolated Flask app, fake Groq client, header-based fake auth).
Run:  python3 -m pytest backend/tests/test_chatbot.py -q   (from project root)
"""
import io, json, os, sys, tempfile, time, sqlite3
import pytest
from flask import Flask, g, request, jsonify

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import chatbot

PAPER_TEXT = [
    ["Adaptive Graph Attention for Traffic Forecasting", "Jane Doe, John Roe", "Abstract",
     "We study short-term traffic forecasting and propose AGAT, an adaptive graph attention network. "
     "The research problem is that fixed road graphs cannot capture dynamic spatial dependencies between sensors. "
     "AGAT learns the adjacency matrix jointly with the forecasting objective and reduces MAE by 7.4 percent on METR-LA.",
     "Keywords: traffic forecasting, graph neural networks", "1. Introduction",
     "Traffic forecasting is essential for intelligent transportation systems. Prior approaches such as DCRNN rely on a predefined graph."],
    ["2. Methodology", "The methodology has three stages. First, sensor readings are normalised. Second, an adaptive adjacency matrix "
     "is learned with a self-attention module. Third, a temporal convolution produces the forecast. Training uses Adam with learning rate 0.001.",
     "3. Experiments", "We evaluate on METR-LA and PEMS-BAY using MAE, RMSE and MAPE. AGAT achieves 2.71 MAE at 15 minutes on METR-LA.",
     "4. Limitations", "The model needs full sensor histories and was not tested on incomplete data. Future work will address missing sensors."],
]


def make_pdf():
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for page in PAPER_TEXT:
        y = 800
        for line in page:
            words, cur = line.split(), ''
            for w in words:
                if len(cur) + len(w) > 90:
                    c.drawString(40, y, cur); y -= 14; cur = ''
                cur += w + ' '
            c.drawString(40, y, cur); y -= 22
        c.showPage()
    c.save()
    return buf.getvalue()


class _Delta:
    def __init__(self, t): self.content = t
class _Choice:
    def __init__(self, t=None, full=None):
        self.delta = _Delta(t); self.message = type('M', (), {'content': full})()
class _Ev:
    def __init__(self, t): self.choices = [_Choice(t)]
class _Resp:
    def __init__(self, text): self.choices = [_Choice(None, text)]

CALLS = []
class FakeGroq:
    def __init__(self, **kw): pass
    @property
    def chat(self): return self
    @property
    def completions(self): return self
    def create(self, **kw):
        CALLS.append(kw)
        sys_msg = kw['messages'][0]['content']
        user = kw['messages'][-1]['content']
        if kw.get('stream'):
            if 'mermaid' in sys_msg and 'TASK: Draw' in sys_msg:
                txt = 'Here is the flow.\n```mermaid\nflowchart TD\n A["Normalise sensor readings"] --> B["Learn adjacency"] --> C["Temporal convolution"]\n```\nThree stages [S1].\nFOLLOWUPS: What data? | Which metrics?'
            elif 'EXCERPTS' in sys_msg and 'adaptive graph attention' in sys_msg.lower() and 'problem' in user.lower():
                txt = 'The problem is that fixed graphs miss dynamic dependencies [S1]. FOLLOWUPS: How is it solved? | Results?'
            else:
                txt = 'The paper excerpts I retrieved do not state this. FOLLOWUPS: Ask something else'
            return iter([_Ev(txt[i:i + 12]) for i in range(0, len(txt), 12)])
        if 'cards' in sys_msg and 'flashcards' in sys_msg:
            return _Resp(json.dumps({'cards': [{'q': 'What is AGAT?', 'a': 'Adaptive graph attention network', 'page': 1}]}))
        if '"children"' in sys_msg:
            return _Resp('```json\n' + json.dumps({'title': 'AGAT', 'children': [{'label': 'Problem', 'detail': 'Fixed graphs', 'page': 1, 'children': []}]}) + '\n```')
        return _Resp('{}')


@pytest.fixture()
def env(monkeypatch):
    tmp = tempfile.mkdtemp()
    dbp = os.path.join(tmp, 't.db')
    def get_db():
        c = sqlite3.connect(dbp, timeout=10); c.row_factory = sqlite3.Row; return c
    def fake_auth(fn):
        import functools
        @functools.wraps(fn)
        def w(*a, **k):
            h = request.headers.get('Authorization', '')
            if not h.startswith('Bearer '):
                return jsonify({'error': 'no auth'}), 401
            g.uid = h[7:]
            return fn(*a, **k)
        return w
    app = Flask(__name__)
    chatbot.register_chatbot_routes(app, get_db, fake_auth, 'test-model', groq_cls=FakeGroq, upload_root=tmp)
    CALLS.clear()
    return app.test_client()


def H(u='alice'): return {'Authorization': 'Bearer ' + u}


def upload(c, user='alice', data=None, name='paper.pdf'):
    r = c.post('/api/chatbot/documents/upload', headers=H(user), data={'file': (io.BytesIO(make_pdf() if data is None else data), name)},
               content_type='multipart/form-data')
    return r


def wait_ready(c, doc_id, user='alice', timeout=15):
    t = time.time()
    while time.time() - t < timeout:
        d = c.get(f'/api/chatbot/documents/{doc_id}', headers=H(user)).get_json()['document']
        if d['status'] in ('ready', 'failed'):
            return d
        time.sleep(0.1)
    raise AssertionError('processing timed out')


def sse_events(resp):
    out = []
    for block in resp.get_data(as_text=True).split('\n\n'):
        if block.strip():
            ev = block.split('\n')[0].replace('event: ', '')
            out.append((ev, json.loads(block.split('data: ', 1)[1])))
    return out


def chat(c, msg, user='alice', **kw):
    body = {'api_key': 'gsk_x', 'message': msg, **kw}
    return c.post('/api/chatbot/chat', headers=H(user), json=body)


def test_requires_auth(env):
    assert env.get('/api/chatbot/documents').status_code == 401
    assert env.post('/api/chatbot/chat', json={}).status_code == 401


def test_upload_processing_and_metadata(env):
    r = upload(env)
    assert r.status_code == 202
    doc = r.get_json()['document']
    assert doc['status'] in ('uploading', 'extracting', 'indexing', 'ready')
    d = wait_ready(env, doc['id'])
    assert d['status'] == 'ready' and d['page_count'] == 2 and d['chunk_count'] >= 2
    assert d['title'] == 'Adaptive Graph Attention for Traffic Forecasting' and d['authors'] == 'Jane Doe, John Roe'
    assert 'adaptive graph attention network' in d['abstract'].lower()
    assert 'traffic forecasting' in d['keywords'].lower()


def test_upload_validation(env):
    assert upload(env, data=b'not a pdf', name='x.pdf').status_code == 400
    assert upload(env, data=b'x', name='x.exe').status_code == 400
    assert upload(env, data=b'', name='x.pdf').status_code == 400
    # a PDF with no text (scanned) fails with a useful recovery message, never "ready"
    from reportlab.pdfgen import canvas
    b = io.BytesIO(); cv = canvas.Canvas(b); cv.rect(10, 10, 50, 50); cv.showPage(); cv.save()
    doc = upload(env, data=b.getvalue()).get_json()['document']
    d = wait_ready(env, doc['id'])
    assert d['status'] == 'failed' and 'OCR' in d['status_detail']


def test_grounded_chat_with_citation_and_followups(env):
    doc = upload(env).get_json()['document']; wait_ready(env, doc['id'])
    r = chat(env, 'What is the main research problem in this paper?', doc_ids=[doc['id']])
    assert r.mimetype == 'text/event-stream'
    ev = sse_events(r)
    meta = dict(ev)['meta']; done = dict(ev)['done']
    assert meta['sources'] and meta['sources'][0]['page'] in (1, 2)
    assert '[S1]' in done['content'] and 'FOLLOWUPS' not in done['content']
    assert done['followups'] == ['How is it solved?', 'Results?']
    # the model really received paper text as context
    assert 'adaptive graph attention' in CALLS[-1]['messages'][0]['content'].lower()
    # persisted -> restorable after refresh
    conv = env.get(f"/api/chatbot/conversations/{meta['conversation_id']}", headers=H()).get_json()
    assert [m['role'] for m in conv['messages']] == ['user', 'assistant']
    assert conv['messages'][1]['sources']


def test_followup_keeps_history_and_missing_info_honest(env):
    doc = upload(env).get_json()['document']; wait_ready(env, doc['id'])
    m1 = dict(sse_events(chat(env, 'What is the main research problem?', doc_ids=[doc['id']])))['meta']
    r = chat(env, 'What GPU model was used?', conversation_id=m1['conversation_id'])
    done = dict(sse_events(r))['done']
    assert 'do not state this' in done['content']
    hist = [m['content'] for m in CALLS[-1]['messages'] if m['role'] != 'system']
    assert any('research problem' in h for h in hist) and hist[-1] == 'What GPU model was used?'


def test_mindmap_flashcards_and_diagram_routes(env):
    doc = upload(env).get_json()['document']; wait_ready(env, doc['id'])
    r = chat(env, 'Create a mind map of this paper.', doc_ids=[doc['id']])
    d = r.get_json()
    assert d['message']['kind'] == 'mindmap'
    mm = json.loads(d['message']['content'])
    assert mm['children'][0]['label'] == 'Problem'
    r = chat(env, 'Make flashcards', conversation_id=d['conversation_id'])
    assert r.get_json()['message']['kind'] == 'flashcards'
    r = chat(env, 'Explain the methodology using a flowchart.', conversation_id=d['conversation_id'])
    done = dict(sse_events(r))['done']
    assert '```mermaid' in done['content'] and 'flowchart TD' in done['content']


def test_isolation_between_users(env):
    doc = upload(env, 'alice').get_json()['document']; wait_ready(env, doc['id'])
    m = dict(sse_events(chat(env, 'What is the main research problem?', doc_ids=[doc['id']])))['meta']
    cid = m['conversation_id']
    assert env.get(f'/api/chatbot/documents/{doc["id"]}', headers=H('bob')).status_code == 404
    assert env.delete(f'/api/chatbot/documents/{doc["id"]}', headers=H('bob')).status_code == 404
    assert env.get(f'/api/chatbot/conversations/{cid}', headers=H('bob')).status_code == 404
    assert env.delete(f'/api/chatbot/conversations/{cid}', headers=H('bob')).status_code == 404
    assert env.get('/api/chatbot/documents', headers=H('bob')).get_json()['total'] == 0
    assert env.get(f'/api/chatbot/documents/{doc["id"]}/page/1', headers=H('bob')).status_code == 404
    # bob chatting with alice's doc id gets no paper context
    chat(env, 'What is the main research problem?', user='bob', doc_ids=[doc['id']]).get_data()
    assert 'adaptive graph attention' not in CALLS[-1]['messages'][0]['content'].lower()


def test_conversation_management_and_notes(env):
    doc = upload(env).get_json()['document']; wait_ready(env, doc['id'])
    m = dict(sse_events(chat(env, 'What is the main research problem?', doc_ids=[doc['id']])))['meta']
    cid = m['conversation_id']
    assert env.patch(f'/api/chatbot/conversations/{cid}', headers=H(), json={'title': 'My chat', 'pinned': True}).get_json()['conversation']['pinned']
    assert env.get('/api/chatbot/conversations?q=research', headers=H()).get_json()['conversations']
    assert not env.get('/api/chatbot/conversations?q=zzzzqq', headers=H()).get_json()['conversations']
    assert b'# My chat' in env.get(f'/api/chatbot/conversations/{cid}/export', headers=H()).data
    nid = env.post('/api/chatbot/notes', headers=H(), json={'content': '## Notes\n- a'}).get_json()['id']
    assert len(env.get('/api/chatbot/notes', headers=H()).get_json()['notes']) == 1
    assert env.get('/api/chatbot/notes', headers=H('bob')).get_json()['notes'] == []
    assert env.delete(f'/api/chatbot/notes/{nid}', headers=H('bob')).status_code == 404
    assert env.delete(f'/api/chatbot/notes/{nid}', headers=H()).status_code == 200
    assert env.delete(f'/api/chatbot/conversations/{cid}', headers=H()).status_code == 200
    assert env.get(f'/api/chatbot/conversations/{cid}', headers=H()).status_code == 404


def test_edit_and_regenerate_truncate(env):
    doc = upload(env).get_json()['document']; wait_ready(env, doc['id'])
    ev = dict(sse_events(chat(env, 'What is the main research problem?', doc_ids=[doc['id']])))
    cid, umid = ev['meta']['conversation_id'], ev['meta']['user_message_id']
    chat(env, 'What is the main research problem here?', conversation_id=cid, truncate_from=umid).get_data()
    msgs = env.get(f'/api/chatbot/conversations/{cid}', headers=H()).get_json()['messages']
    assert len(msgs) == 2 and msgs[0]['content'].endswith('here?')


def test_delete_document_removes_index(env):
    doc = upload(env).get_json()['document']; wait_ready(env, doc['id'])
    assert env.delete(f'/api/chatbot/documents/{doc["id"]}', headers=H()).status_code == 200
    assert env.get('/api/chatbot/documents', headers=H()).get_json()['total'] == 0


def test_requires_key_and_validates_message(env):
    assert env.post('/api/chatbot/chat', headers=H(), json={'message': 'hi'}).status_code == 400
    assert chat(env, '').status_code == 400
    assert chat(env, 'x' * 7000).status_code == 400


def test_chunker_keeps_pages_and_sections():
    ch = chatbot.build_chunks(["Abstract\nSome text here.", "2. Methodology\nWe do things."])
    assert ch[0][0] == 1 and ch[-1][0] == 2 and ch[-1][1].lower().startswith('method')
