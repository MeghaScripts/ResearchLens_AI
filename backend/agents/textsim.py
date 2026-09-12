"""
Minimal pure-Python TF-IDF + cosine similarity.

Used by the RAG Research Assistant Agent (retrieval over uploaded paper
text) and the Novelty Verification Agent (similarity between a proposed
idea and fetched papers). Deliberately dependency-free (no numpy/sklearn)
to keep the deploy footprint on Render's free tier small — this is a
few dozen lines of math, not a few hundred MB of wheels.
"""
import math
import re
from collections import Counter

_STOPWORDS = set([
    'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of',
    'with', 'is', 'are', 'was', 'were', 'be', 'been', 'have', 'has', 'had',
    'do', 'does', 'did', 'will', 'would', 'could', 'should', 'this', 'that',
    'these', 'those', 'from', 'by', 'as', 'not', 'it', 'its', 'we', 'our',
    'they', 'their', 'you', 'your', 'i', 'my',
])


def tokenize(text):
    words = re.findall(r"\b[a-z0-9]{2,}\b", (text or '').lower())
    return [w for w in words if w not in _STOPWORDS]


def tfidf_vectors(docs):
    """docs: list[str] -> list[dict[str, float]], one TF-IDF vector per doc."""
    tokenized = [tokenize(d) for d in docs]
    df = Counter()
    for toks in tokenized:
        for t in set(toks):
            df[t] += 1
    n = len(docs)
    vectors = []
    for toks in tokenized:
        tf = Counter(toks)
        vec = {}
        for t, c in tf.items():
            idf = math.log((n + 1) / (df[t] + 1)) + 1
            vec[t] = c * idf
        vectors.append(vec)
    return vectors


def cosine(v1, v2):
    common = set(v1) & set(v2)
    if not common:
        return 0.0
    dot = sum(v1[t] * v2[t] for t in common)
    n1 = math.sqrt(sum(x * x for x in v1.values()))
    n2 = math.sqrt(sum(x * x for x in v2.values()))
    if n1 == 0 or n2 == 0:
        return 0.0
    return dot / (n1 * n2)


def most_similar(query, corpus, top_n=5):
    """query: str, corpus: list[str] -> [(index, score), ...] sorted desc,
    scores in [0, 1]. Returns [] if corpus is empty."""
    if not corpus:
        return []
    vectors = tfidf_vectors([query] + list(corpus))
    qv = vectors[0]
    scores = [(i, cosine(qv, vectors[i + 1])) for i in range(len(corpus))]
    scores.sort(key=lambda x: -x[1])
    return scores[:top_n]


def chunk_text(text, chunk_size=900, overlap=150):
    """Splits text into overlapping character chunks for retrieval."""
    text = text or ''
    if not text:
        return []
    chunks = []
    i = 0
    step = max(1, chunk_size - overlap)
    while i < len(text):
        chunk = text[i:i + chunk_size].strip()
        if chunk:
            chunks.append(chunk)
        i += step
    return chunks


# ─── Grounded, domain-aware novelty score ──────────────────────────
#
# Replaces an LLM's self-reported "novelty_score" (which has no real
# relationship to the actual literature — nothing stops the model from
# just echoing back a plausible-looking number) with a REAL score
# computed the same honest way NoveltyVerificationAgent already does
# for standalone idea pre-checks: TF-IDF cosine similarity between the
# subject text and the real papers this run actually collected.
# novelty_score = 100 minus the single closest match's similarity —
# a paper/idea that's textually near-identical to something already in
# the corpus scores near 0; one with no close match scores near 100.

MIN_DOMAIN_CORPUS = 3  # below this, a domain-filtered comparison isn't meaningful


def _paper_domain_text(paper):
    """Everything on a paper record that plausibly signals its
    domain/sub-field, for keyword matching against a classified
    primary/secondary domain — never a guess, just the paper's own
    already-collected metadata."""
    return ' '.join(str(paper.get(f, '') or '') for f in
                     ('venue', 'category', 'categories', 'source')).lower()


def compute_grounded_novelty(subject_text, papers, domain_terms=None):
    """
    `subject_text`: the paper/idea/topic being assessed for novelty.
    `papers`: the real papers this run actually collected (title +
    abstract used as each paper's comparison text).
    `domain_terms`: optional list of real domain/sub-domain name(s) —
    e.g. from DomainClassificationAgent's primary_domain, or the
    user's own explicit primary_domain/secondary_domains form field —
    used to scope the comparison to papers that plausibly sit in the
    same domain, rather than diluting the similarity check against the
    entire fetched corpus (which may span tangential matches from a
    broad topic search).

    Domain scoping is itself honest about its own limits: it only
    narrows the corpus when at least MIN_DOMAIN_CORPUS papers actually
    match a domain term in their own venue/category/source metadata —
    below that, a "domain-scoped" comparison against 1-2 papers would
    be a fairly meaningless signal to report as domain-grounded, so it
    transparently falls back to the full fetched corpus instead
    (`novelty_domain_scoped: False` reflects that back to the caller).

    Returns a dict — never a guessed number:
      novelty_score: float 0-100, or None if there was nothing real to
        compare against (no papers collected, or subject_text empty).
      novelty_score_basis: 'tfidf_similarity' — always the same real
        method; kept as a field so a future different method is
        distinguishable in stored/exported analyses.
      novelty_domain_scoped: whether domain filtering actually reduced
        the comparison set (see MIN_DOMAIN_CORPUS above).
      novelty_compared_against: how many papers were in the actual
        comparison pool.
      novelty_closest_matches: the top handful of most-similar real
        papers with their real similarity percentage, so the score is
        never a black box.
    """
    subject_text = (subject_text or '').strip()
    corpus_papers = [p for p in (papers or []) if p.get('title')]
    if not subject_text or not corpus_papers:
        return {
            'novelty_score': None,
            'novelty_score_basis': 'no_comparable_papers',
            'novelty_domain_scoped': False,
            'novelty_compared_against': 0,
            'novelty_closest_matches': [],
        }

    domain_scoped = False
    pool = corpus_papers
    domain_terms = [t.strip().lower() for t in (domain_terms or []) if t and t.strip()]
    if domain_terms:
        filtered = [
            p for p in corpus_papers
            if any(term in _paper_domain_text(p) or term in (p.get('title', '') + ' ' + p.get('abstract', '')).lower()
                   for term in domain_terms)
        ]
        if len(filtered) >= MIN_DOMAIN_CORPUS:
            pool = filtered
            domain_scoped = True

    corpus_texts = [f"{p.get('title', '')}. {p.get('abstract', '')}" for p in pool]
    top = most_similar(subject_text, corpus_texts, top_n=5)
    closest = []
    for idx, score in top:
        if score <= 0:
            continue
        p = pool[idx]
        closest.append({
            'title': p.get('title'), 'year': p.get('year'), 'source': p.get('source'),
            'similarity_pct': round(score * 100, 1),
        })

    if not closest:
        # Every real comparison came back with zero textual overlap —
        # a real (if weak) signal in itself, not "no data".
        novelty_score = 100.0
    else:
        max_sim = max(c['similarity_pct'] for c in closest)
        novelty_score = round(max(0.0, 100.0 - max_sim), 1)

    return {
        'novelty_score': novelty_score,
        'novelty_score_basis': 'tfidf_similarity',
        'novelty_domain_scoped': domain_scoped,
        'novelty_compared_against': len(pool),
        'novelty_closest_matches': closest,
    }