"""
Vector store for paper/document embeddings.

Honest scope note: this is TF-IDF vectors persisted to SQLite, not a
real ANN vector database (FAISS/pgvector/Pinecone). That's a deliberate
choice consistent with textsim.py's existing "dependency-free, small
deploy footprint" philosophy for this app — the corpora here are one
paper's chunks or a few dozen fetched abstracts, not millions of
documents, so brute-force cosine over sparse TF-IDF vectors is fast
enough and needs zero new infrastructure (no separate vector DB service,
no numpy/faiss wheel). If the corpus size ever grows past what
brute-force cosine handles in milliseconds, swap this module's internals
for a real ANN index — the EmbeddingAgent's interface (add/search) stays
the same either way.
"""
import json
from agents.textsim import tfidf_vectors, cosine


def init_embeddings_table(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS embeddings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        collection TEXT NOT NULL,
        item_id TEXT NOT NULL,
        text_preview TEXT,
        vector_json TEXT NOT NULL,
        UNIQUE(collection, item_id)
    )''')
    conn.commit()


class VectorStore:
    def __init__(self, get_db_fn):
        self.get_db = get_db_fn

    def add_batch(self, collection, items):
        """items: list[(item_id: str, text: str)]. Computes TF-IDF vectors
        over this batch (each call's IDF is local to the batch — fine for
        the "score a handful of chunks/papers against one query" use case
        this app has; it is not meant to accumulate a global corpus)."""
        if not items:
            return 0
        ids = [i for i, _ in items]
        texts = [t for _, t in items]
        vectors = tfidf_vectors(texts)
        conn = self.get_db()
        for item_id, text, vec in zip(ids, texts, vectors):
            conn.execute(
                '''INSERT INTO embeddings (collection, item_id, text_preview, vector_json)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(collection, item_id)
                   DO UPDATE SET text_preview=excluded.text_preview, vector_json=excluded.vector_json''',
                (collection, item_id, text[:200], json.dumps(vec))
            )
        conn.commit()
        return len(items)

    def search(self, collection, query, top_n=5):
        conn = self.get_db()
        rows = conn.execute(
            'SELECT item_id, text_preview, vector_json FROM embeddings WHERE collection=?',
            (collection,)
        ).fetchall()
        if not rows:
            return []
        qv = tfidf_vectors([query])[0]
        scored = []
        for r in rows:
            vec = json.loads(r['vector_json'])
            score = cosine(qv, vec)
            scored.append({'item_id': r['item_id'], 'preview': r['text_preview'], 'score': round(score, 4)})
        scored.sort(key=lambda x: -x['score'])
        return scored[:top_n]

    def clear_collection(self, collection):
        conn = self.get_db()
        conn.execute('DELETE FROM embeddings WHERE collection=?', (collection,))
        conn.commit()
