"""
Real semantic similarity (architecture-review Phase 1 / Step 1).

Until now the relevance layer's fifth component was TF-IDF cosine
labelled honestly as lexical similarity — accurate, but not what a
"semantic embedding similarity" line in a report should mean. This
module makes that line true: it computes real sentence-embedding
cosine similarity via `sentence-transformers` when the library and a
model are actually available, and falls back to the same TF-IDF cosine
already computed elsewhere in the pipeline when they are not —
labelling every score with exactly which method produced it, so
nothing downstream (or in a viva) can accidentally claim more than
what actually ran.

    Sentence Transformer available?
           │
      ┌────┴────┐
     YES         NO
      │           │
      ▼           ▼
  embedding    tfidf cosine
  cosine       (already computed
  similarity   by compute_relevance_scores)
      │           │
      └─────┬─────┘
            ▼
   {"value": 0.87, "method": "sentence_transformer" | "tfidf_fallback"}

HONESTY NOTES:

 1. The model is loaded AT MOST ONCE per process. If `sentence-
    transformers` isn't installed, or a model can't be downloaded
    (no network to huggingface.co, e.g. in a sandboxed/offline
    deployment), the failure is cached — every subsequent call falls
    back to TF-IDF immediately rather than retrying a slow, doomed
    import/download on every paper.

 2. `sentence-transformers` (and its `torch` dependency) is a genuinely
    heavy install — several hundred MB. It is listed in requirements.txt
    as an OPTIONAL dependency: the app runs correctly, with an honestly
    labelled `tfidf_fallback`, whether or not it's installed. Set
    `DEEP_DISABLE_SEMANTIC_MODEL=1` to force the TF-IDF path even when
    the library IS available (useful on memory-constrained deployments).

 3. This module never claims "semantic similarity" when it only ran
    TF-IDF — `similarity_method` is written onto every paper and into
    the run summary specifically so a report/viva answer can say
    exactly which one happened, not the aspirational one.
"""
import os
import functools

DEFAULT_MODEL_NAME = os.environ.get('DEEP_SEMANTIC_MODEL', 'all-MiniLM-L6-v2')
DISABLED = os.environ.get('DEEP_DISABLE_SEMANTIC_MODEL', '').lower() in ('1', 'true', 'yes')

_model = None
_load_attempted = False
_load_error = None


def _try_load_model():
    """Attempts the import + download/load exactly once per process.
    Returns the loaded model or None. Any failure — missing package,
    no network to huggingface.co, out of memory, corrupt cache — is
    caught and cached as None so it costs one slow attempt, not one
    per paper."""
    global _model, _load_attempted, _load_error
    if _load_attempted:
        return _model
    _load_attempted = True
    if DISABLED:
        _load_error = 'disabled via DEEP_DISABLE_SEMANTIC_MODEL'
        return None
    try:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(DEFAULT_MODEL_NAME)
    except Exception as e:
        _load_error = f'{type(e).__name__}: {e}'
        _model = None
    return _model


def model_status():
    """For the verification-run summary / UI: which method is actually
    in effect right now, and why, without triggering a load attempt
    just to answer the question."""
    if DISABLED:
        return {'available': False, 'method': 'tfidf_fallback',
                'reason': 'disabled via DEEP_DISABLE_SEMANTIC_MODEL', 'model': None}
    if not _load_attempted:
        return {'available': None, 'method': 'not yet determined',
                'reason': 'model not loaded yet', 'model': DEFAULT_MODEL_NAME}
    if _model is not None:
        return {'available': True, 'method': 'sentence_transformer',
                'reason': None, 'model': DEFAULT_MODEL_NAME}
    return {'available': False, 'method': 'tfidf_fallback',
            'reason': _load_error, 'model': DEFAULT_MODEL_NAME}


def embed_batch(texts):
    """Encodes a list of strings in one batched call (far cheaper than
    one call per paper). Returns a list of vectors, or None if the
    model isn't available — callers must check for None and fall back
    to TF-IDF themselves rather than treating an empty-looking result
    as a real embedding of zeros."""
    model = _try_load_model()
    if model is None:
        return None
    try:
        return model.encode([t or '' for t in texts], convert_to_numpy=True,
                            show_progress_bar=False, batch_size=32)
    except Exception as e:
        global _load_error
        _load_error = f'encode failed: {type(e).__name__}: {e}'
        return None


def _cosine_np(a, b):
    import numpy as np
    denom = (np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0:
        return 0.0
    return float(np.dot(a, b) / denom)


def compute_semantic_similarities(query_text, paper_texts, tfidf_fallback_values):
    """
    query_text: str — the combined topic/keywords/research-question query.
    paper_texts: list[str] — one "title + abstract" string per paper,
      same order and length as tfidf_fallback_values.
    tfidf_fallback_values: list[float] — the TF-IDF cosine already
      computed for each paper by compute_relevance_scores (so the
      fallback costs no extra computation and is guaranteed to exist).

    Returns (values, method) where values is a list the same length as
    paper_texts, and method is 'sentence_transformer' or 'tfidf_fallback'
    — ONE method for the whole batch, since either the model is usable
    for this run or it isn't; a per-paper mix would make the "how was
    this computed" answer incoherent.
    """
    n = len(paper_texts)
    if n != len(tfidf_fallback_values):
        raise ValueError('paper_texts and tfidf_fallback_values must be the same length')

    vectors = embed_batch([query_text] + list(paper_texts))
    if vectors is None:
        return list(tfidf_fallback_values), 'tfidf_fallback'

    query_vec = vectors[0]
    values = [max(0.0, min(1.0, _cosine_np(query_vec, vectors[i + 1]))) for i in range(n)]
    return values, 'sentence_transformer'
