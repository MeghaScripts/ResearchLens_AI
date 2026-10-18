"""
Paper quality gate + gap de-duplication / batching helpers (pure Python, no network).

Why this exists
---------------
1. Junk papers reached the gap analysis. The network verification only
   removes papers with *positive* evidence of a problem, only checks the
   top-N papers online, and is abandoned entirely if it runs long - so
   keyboard-mash records ("dfgsdf"), "Acknowledgment to the Reviewers ..."
   notices and similar non-research items sailed through. `local_quality_filter`
   is an instant, deterministic gate that ALWAYS runs, before anything is sent
   to an AI model or contributed to a workspace library.

2. 500 papers produced ~10 gaps. The gap prompt only ever saw a handful of
   huge batches, each asked for 3-6 gaps, then everything was cut at 15 and
   de-duplicated by the first 60 characters of the title. `plan_gap_batches`
   groups papers by theme so every batch is coherent, and `dedupe_gaps`
   removes only genuine near-duplicates (TF-IDF similarity) and ranks the
   rest by severity and confidence.
"""
import math
import os
import re
from collections import Counter

from agents.textsim import tokenize, tfidf_vectors, cosine

MAX_GAPS = int(os.environ.get('MAX_GAPS', '0'))                      # 0 = no cap: keep every distinct, grounded gap
GAP_BATCH_TARGET = int(os.environ.get('GAP_BATCH_TARGET', '25'))      # papers per gap-analysis call
GAP_MAX_BATCHES = int(os.environ.get('GAP_MAX_BATCHES', '0'))         # 0 = as many batches as the corpus needs
GAP_DUP_SIMILARITY = float(os.environ.get('GAP_DUP_SIMILARITY', '0.6'))
REQUIRE_TOPIC_OVERLAP = os.environ.get('QUALITY_REQUIRE_TOPIC_OVERLAP', '1') != '0'

# ───────────────────────── paper quality gate ─────────────────────────
# Unambiguous administrative starts (a real paper never begins like this)...
_ADMIN_START = re.compile(
    r'^\s*(acknowledg(e)?ments?\b|list of reviewers|reviewer acknowledg|erratum\b|errata\b|corrigendum\b|'
    r'correction to\b|retraction (notice|note)\b|call for papers?\b|in memoriam\b|obituary\b|'
    r'issue information\b|publisher.?s note\b|front matter\b|back matter\b|table of contents\b|'
    r'masthead\b|title page\b|editor.?s? (note|letter)\b|thanks to (our |the )?reviewers)', re.I)
# ...and ambiguous words ("Cover Trees", "Index Selection", "Contents-based retrieval" are real
# papers) that only count when the WHOLE title is just that, e.g. "Editorial", "Author Index".
_ADMIN_SHORT = re.compile(
    r'^\s*(editorial( board)?|index|(author|subject) index|contents|cover( page)?|preface|foreword|'
    r'announcements?|reviewers?|withdrawn|retraction|errata)\b', re.I)
_ADMIN_ANYWHERE = re.compile(
    r'\b(acknowledg(e)?ments? (to|of) (the )?(our )?reviewers?|thanks to (our |the )?reviewers|'
    r'list of reviewers|reviewer acknowledg(e)?ments?)\b', re.I)
_FILENAME_OR_URL = re.compile(r'(^https?://|\.(pdf|docx?|pptx?|txt|tex)\s*$|^untitled\b|^test(ing)?\b\s*\d*$)', re.I)
_GENERIC_TOPIC_WORDS = {
    'using', 'based', 'approach', 'approaches', 'method', 'methods', 'study', 'analysis', 'paper', 'research',
    'novel', 'new', 'towards', 'toward', 'via', 'use', 'review', 'survey', 'framework', 'case', 'application',
    'applications', 'improving', 'improved', 'evaluation', 'idea', 'about', 'into', 'over', 'under'}


def _alpha_words(text):
    return re.findall(r"[A-Za-z]{2,}", text or '')


def _vowel_ratio(word):
    letters = [c for c in word.lower() if c.isalpha()]
    return (sum(c in 'aeiouy' for c in letters) / len(letters)) if letters else 0.0


def _gibberish_word(word):
    w = word.lower()
    if len(w) >= 4 and _vowel_ratio(w) < 0.10:
        return True
    if len(w) >= 6 and re.search(r'(.)\1\1', w):          # "aaaa", "hhhh"
        return True
    if len(w) >= 5 and any(row.find(w[:4]) >= 0 for row in ('qwertyuiop', 'asdfghjkl', 'zxcvbnm')):
        return True                                       # keyboard-row mash: "asdfg", "zxcvb"
    if len(w) >= 6 and len(set(w)) <= len(w) / 2.5:      # "dafdfafd": 3 distinct letters in 8
        return True
    return False


def _norm_title(t):
    return re.sub(r'[^a-z0-9]+', ' ', (t or '').lower()).strip()


def _stem_match(term, word):
    if term == word:
        return True
    return len(term) >= 5 and len(word) >= 5 and term[:5] == word[:5]


def topic_terms(topic):
    return [t for t in dict.fromkeys(tokenize(topic or '')) if len(t) >= 3 and t not in _GENERIC_TOPIC_WORDS]


def junk_reason(paper, terms=None):
    """Return a short reason string if `paper` is not a usable research paper, else None.
    Deterministic and offline; every rule is something a reviewer could explain."""
    title = (paper.get('title') or '').strip()
    abstract = (paper.get('abstract') or paper.get('summary') or '')
    abstract = abstract if isinstance(abstract, str) else ''
    if not title:
        return 'empty title'
    short_admin = bool(_ADMIN_SHORT.match(title)) and len(_alpha_words(title)) <= 4
    if _ADMIN_START.search(title) or _ADMIN_ANYWHERE.search(title) or short_admin:
        return 'non-research item (editorial / acknowledgment / reviewer list / front matter)'
    if _FILENAME_OR_URL.search(title):
        return 'title is a file name, URL or placeholder'
    words = _alpha_words(title)
    if not words or all(len(w) <= 2 for w in words):
        return 'title has no real words'
    gib = [w for w in words if _gibberish_word(w)]
    if len(gib) / len(words) >= 0.5:
        return 'gibberish title'
    if len(words) < 3 and len(abstract.strip()) < 80:
        return 'title too short and no abstract'
    has_meta = bool(len(abstract.strip()) >= 40 or paper.get('authors') or paper.get('year')
                    or paper.get('doi') or paper.get('url') or paper.get('venue') or paper.get('arxiv_id'))
    if not has_meta:
        return 'no bibliographic metadata (no abstract, authors, year, DOI, URL or venue)'
    if REQUIRE_TOPIC_OVERLAP and terms:
        hay = set(tokenize(title + ' ' + abstract + ' ' + ' '.join(map(str, paper.get('keywords') or []))))
        if not any(_stem_match(t, w) for t in terms for w in hay):
            return 'no term of the research topic appears in its title or abstract'
    return None


def local_quality_filter(papers, topic=''):
    """-> (kept, dropped). `dropped` items: {title, year, source, reason}.
    Never returns an empty `kept` for a non-empty input (a safety net so a
    misparsed topic can't wipe out a run); in that case nothing is dropped."""
    terms = topic_terms(topic)
    kept, dropped, seen = [], [], set()
    for p in papers or []:
        reason = junk_reason(p, terms)
        if not reason:
            key = _norm_title(p.get('title'))
            if key in seen:
                reason = 'duplicate title'
            else:
                seen.add(key)
        if reason:
            dropped.append({'title': p.get('title'), 'year': p.get('year'), 'source': p.get('source'), 'reason': reason})
        else:
            kept.append(p)
    if papers and not kept:
        return list(papers), []
    return kept, dropped


# ───────────────────────── gap de-duplication ─────────────────────────
_SEV = {'critical': 0, 'major': 1, 'moderate': 2}


def _confidence(g):
    try:
        return float(re.sub(r'[^0-9.]', '', str(g.get('confidence_score', 0))) or 0)
    except ValueError:
        return 0.0


def dedupe_gaps(gaps, max_gaps=None, similarity=None):
    """Drop only genuine near-duplicates (same title prefix, or TF-IDF similarity of
    title+description >= `similarity`), rank by severity then confidence, cap at
    `max_gaps` (default MAX_GAPS)."""
    cap = max_gaps or MAX_GAPS or None      # None = unlimited
    sim_limit = similarity if similarity is not None else GAP_DUP_SIMILARITY
    items = [g for g in gaps or [] if isinstance(g, dict) and (g.get('gap_title') or '').strip()]
    order = sorted(range(len(items)),
                   key=lambda i: (_SEV.get(str(items[i].get('severity', '')).lower(), 3), -_confidence(items[i]), i))
    texts = [((items[i].get('gap_title') or '') + ' ') * 2 + (items[i].get('description') or '') for i in range(len(items))]
    vecs = tfidf_vectors(texts) if texts else []
    kept_idx, seen_keys = [], set()
    for i in order:
        key = re.sub(r'[^a-z0-9]+', ' ', items[i]['gap_title'].lower()).strip()[:60]
        if key in seen_keys:
            continue
        if any(cosine(vecs[i], vecs[j]) >= sim_limit for j in kept_idx):
            continue
        seen_keys.add(key)
        kept_idx.append(i)
        if cap and len(kept_idx) >= cap:
            break
    return [items[i] for i in kept_idx]


# ───────────────────────── theme batching ─────────────────────────
def _paper_text(p):
    abs_ = p.get('abstract') or ''
    return (p.get('title') or '') + ' ' + (abs_[:300] if isinstance(abs_, str) else '')


def _centroid(vecs):
    c = Counter()
    for v in vecs:
        for t, w in v.items():
            c[t] += w
    n = math.sqrt(sum(x * x for x in c.values())) or 1.0
    return {t: w / n for t, w in c.items()}


def cluster_papers(papers, k, iters=6):
    """Small deterministic k-means on TF-IDF vectors (cosine). -> list of (papers, vectors)."""
    n = len(papers)
    vecs = tfidf_vectors([_paper_text(p) for p in papers])
    if k <= 1 or n <= k:
        return [(list(papers), vecs)]
    # farthest-first initial centroids (deterministic)
    cents = [vecs[0]]
    while len(cents) < k:
        best_i, best_d = None, -1.0
        for i, v in enumerate(vecs):
            d = 1.0 - max(cosine(v, c) for c in cents)
            if d > best_d:
                best_i, best_d = i, d
        cents.append(vecs[best_i])
    assign = [0] * n
    for _ in range(iters):
        new = [max(range(k), key=lambda c: cosine(v, cents[c])) for v in vecs]
        if new == assign and _ > 0:
            break
        assign = new
        for c in range(k):
            members = [vecs[i] for i in range(n) if assign[i] == c]
            if members:
                cents[c] = _centroid(members)
    groups = []
    for c in range(k):
        idx = [i for i in range(n) if assign[i] == c]
        if idx:
            groups.append(([papers[i] for i in idx], [vecs[i] for i in idx]))
    return groups


def _theme(vecs, top=6):
    c = Counter()
    for v in vecs:
        for t, w in v.items():
            c[t] += w
    return ', '.join(t for t, _ in c.most_common(top))


def plan_gap_batches(papers, target=None, max_batches=None):
    """-> list of (batch_papers, theme_terms). Small corpora stay a single batch (unchanged
    behaviour); large ones are grouped by theme so each LLM call sees a coherent set."""
    target = target or GAP_BATCH_TARGET
    max_batches = max_batches or GAP_MAX_BATCHES or None    # None = unlimited
    n = len(papers)
    if n <= int(target * 1.5):
        return [(list(papers), '')]
    if max_batches:
        target = max(target, math.ceil(n / max_batches))
    k = max(2, math.ceil(n / target))
    if max_batches:
        k = min(max_batches, k)
    out = []
    for group, vecs in cluster_papers(papers, k):
        theme = _theme(vecs)
        step = int(target * 2)
        if len(group) <= step:
            out.append((group, theme))
        else:                                              # split oversized clusters evenly
            parts = math.ceil(len(group) / step)
            size = math.ceil(len(group) / parts)
            for i in range(0, len(group), size):
                out.append((group[i:i + size], theme))
    return out


# ───────────────────────── evidence grounding (no invented gaps) ─────────────────────────
def _title_tokens(t):
    return {w for w in tokenize(t or '') if len(w) >= 3 and w not in _GENERIC_TOPIC_WORDS}


def _match_paper(cited, index):
    """Map a title cited by the model to a REAL collected paper, or None.
    index: list of (paper, normalized_title, token_set)."""
    key = _norm_title(cited)
    if len(key) < 8:
        return None
    toks = _title_tokens(cited)
    best, best_score = None, 0.0
    for paper, nt, pt in index:
        if key == nt or (len(key) >= 12 and (key in nt or nt in key)):
            return paper
        if toks and pt:
            j = len(toks & pt) / len(toks | pt)
            if j > best_score:
                best, best_score = paper, j
    return best if best_score >= 0.6 else None


def ground_gaps(gaps, papers, min_description_chars=40):
    """Keep only gaps that are backed by real, collected papers.
    A gap is REJECTED when: it cites no paper that exists in the collected set; its text shares no
    substantive term with any cited paper's title/abstract; or it has no real description.
    Kept gaps get canonical evidence titles (+ links), `evidence_verified_count` and `grounded=True`.
    -> (kept, rejected) where rejected items are {gap_title, reason}."""
    index = [(p, _norm_title(p.get('title')), _title_tokens(p.get('title'))) for p in papers or [] if p.get('title')]
    kept, rejected = [], []
    for g in gaps or []:
        title = (g.get('gap_title') or '').strip()
        if not title:
            continue
        desc = (g.get('description') or '').strip()
        if len(desc) < min_description_chars:
            rejected.append({'gap_title': title, 'reason': 'no real description'})
            continue
        cited = g.get('evidence_papers') or []
        if isinstance(cited, str):
            cited = [cited]
        matched, seen = [], set()
        for c in cited:
            if not isinstance(c, str):
                continue
            paper = _match_paper(c, index)
            if paper is not None and id(paper) not in seen:
                seen.add(id(paper))
                matched.append(paper)
        if not matched:
            rejected.append({'gap_title': title, 'reason': 'cites no paper that exists in the collected set'})
            continue
        gap_terms = _title_tokens(title + ' ' + desc)
        ev_terms = set()
        for p in matched:
            ab = p.get('abstract') or ''
            ev_terms |= _title_tokens((p.get('title') or '') + ' ' + (ab if isinstance(ab, str) else ''))
        if gap_terms and not any(_stem_match(a, b) for a in gap_terms for b in ev_terms):
            rejected.append({'gap_title': title, 'reason': 'evidence papers are unrelated to the gap'})
            continue
        g = dict(g)
        g['evidence_papers'] = [p.get('title') for p in matched]
        g['evidence_links'] = [p.get('url') or (('https://doi.org/' + p['doi']) if p.get('doi') else '') for p in matched]
        g['evidence_verified_count'] = len(matched)
        g['grounded'] = True
        kept.append(g)
    return kept, rejected
