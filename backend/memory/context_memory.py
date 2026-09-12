"""
SQLite-backed memory store: conversation history, research findings, gap
notes, and proposal drafts, all scoped per (owner_uid, analysis_id).

This is deliberately just the existing `analyses` DB pattern extended to
a generic key/value shape rather than a new subsystem — same sqlite3
connection, same conn.execute() style as the rest of app.py. A "memory
agent" in an enterprise write-up usually implies a dedicated memory
service; here it's one more table, which is the right amount of
infrastructure for a single-process Flask app.

Four memory *kinds* are supported (matching the four the enterprise
review asked for): 'conversation', 'research', 'gap', 'proposal'. Kind
is just a namespace — callers choose the key within it (e.g. kind=
'conversation', key='thread_1').
"""
import json
import datetime

_KINDS = {'conversation', 'research', 'gap', 'proposal'}


def init_memory_table(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS agent_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_uid TEXT NOT NULL,
        analysis_id INTEGER,
        kind TEXT NOT NULL,
        key TEXT NOT NULL,
        value_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(owner_uid, analysis_id, kind, key)
    )''')
    conn.commit()


class ContextMemory:
    def __init__(self, get_db_fn):
        # get_db_fn: () -> sqlite3.Connection, same helper app.py already
        # uses (get_db()), so this shares the app's one DB file/pooling
        # behavior rather than opening a second connection strategy.
        self.get_db = get_db_fn

    def _check_kind(self, kind):
        if kind not in _KINDS:
            raise ValueError(f"Unknown memory kind '{kind}', expected one of {sorted(_KINDS)}")

    def set(self, owner_uid, kind, key, value, analysis_id=None):
        self._check_kind(kind)
        conn = self.get_db()
        conn.execute(
            '''INSERT INTO agent_memory (owner_uid, analysis_id, kind, key, value_json, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(owner_uid, analysis_id, kind, key)
               DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at''',
            (owner_uid, analysis_id, kind, key, json.dumps(value),
             datetime.datetime.now().isoformat())
        )
        conn.commit()

    def get(self, owner_uid, kind, key, analysis_id=None, default=None):
        self._check_kind(kind)
        conn = self.get_db()
        row = conn.execute(
            'SELECT value_json FROM agent_memory WHERE owner_uid=? AND kind=? AND key=? AND (analysis_id=? OR (analysis_id IS NULL AND ? IS NULL))',
            (owner_uid, kind, key, analysis_id, analysis_id)
        ).fetchone()
        return json.loads(row['value_json']) if row else default

    def append_conversation(self, owner_uid, thread_key, role, content, analysis_id=None, max_turns=40):
        """Convenience helper for the common case: append a turn to a
        conversation thread, keeping only the most recent `max_turns`."""
        history = self.get(owner_uid, 'conversation', thread_key, analysis_id, default=[])
        history.append({'role': role, 'content': content,
                         'at': datetime.datetime.now().isoformat()})
        history = history[-max_turns:]
        self.set(owner_uid, 'conversation', thread_key, history, analysis_id)
        return history

    def list_keys(self, owner_uid, kind, analysis_id=None):
        self._check_kind(kind)
        conn = self.get_db()
        rows = conn.execute(
            'SELECT key, updated_at FROM agent_memory WHERE owner_uid=? AND kind=? AND (analysis_id=? OR (analysis_id IS NULL AND ? IS NULL))',
            (owner_uid, kind, analysis_id, analysis_id)
        ).fetchall()
        return [{'key': r['key'], 'updated_at': r['updated_at']} for r in rows]
