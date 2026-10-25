"""
Three-level paper verification: AUTHENTICITY -> RELEVANCE -> QUALITY.

This module answers the question "should this paper be allowed into the
ranking at all?", which the collection pipeline (deep_collection.py)
deliberately does not ask — that module only ranks by impact. Here every
paper that survived Stage 2 is put through:

  Level 1  AUTHENTICITY  Does this paper actually exist, and do
                         independent scholarly registries agree about
                         what it is? Persistent identifier -> DOI
                         resolution via Crossref -> cross-source
                         metadata agreement (title / authors / year /
                         venue / publisher) -> retraction status.
  Level 2  RELEVANCE     Is it about the user's topic at all, or merely
                         a genuine paper the search engine happened to
                         return?
  Level 3  QUALITY       Venue, publication type, metadata
                         completeness, methodology evidence, author
                         and affiliation information, citation impact
                         and recency.

Four scores are kept SEPARATELY on every paper — `authenticity_score`,
`relevance_score`, `quality_score` and the existing `impact_score` —
plus an `overall_score` that combines them 20/25/55 (authenticity /
relevance / quality, with impact folded into quality). Separate scores
are the point: "rejected because authenticity 41" is explainable in a
viva, "score 41" is not.

HONESTY NOTES — read these before claiming anything about this module:

 1. No fabricated evidence, ever. Every check either found real data
    from a real API response or is recorded as `null`/not-applicable.
    A check that could not run is EXCLUDED from the score and the
    remaining weights are renormalized (same approach
    deep_collection.compute_impact_score already uses), never
    zero-filled and never assumed to pass.

 2. Absence of evidence is not evidence of fraud. A paper that could
    not be independently corroborated is capped (see
    UNCORROBORATED_CEILING) and marked UNVERIFIED — not deleted.
    Papers are only actively REJECTED on POSITIVE evidence of a
    problem: a DOI that does not resolve, metadata that contradicts
    across independent registries, a retraction, or an excluded
    publication type. This is a deliberate deviation from a pure
    "authenticity < 75 -> reject" rule, which would silently delete
    every legitimate DBLP/ERIC/Internet-Archive record that simply has
    no DOI to check. The numeric threshold is still enforced
    (AUTHENTICITY_REJECT_THRESHOLD) — it just downgrades to
    `needs_review` instead of deleting when the low score is caused by
    missing checks rather than failed ones. Both behaviours are
    recorded per paper so the distinction is auditable.

 3. The relevance layer's fifth component, `semantic_similarity`, uses
    real sentence-transformer embeddings (semantic_similarity.py) when
    that library and a model are available in this deployment, and
    falls back to TF-IDF cosine (agents/textsim.py — lexical, not
    semantic) when they are not. EVERY paper carries
    `relevance_similarity_method` ('sentence_transformer' or
    'tfidf_fallback') recording which actually ran for that run — do
    not describe a report/viva result as "semantic embedding
    similarity" unless that field actually says
    'sentence_transformer'; when it says 'tfidf_fallback', describe it
    as lexical/statistical similarity, because that is what ran.

 4. Network verification costs one Crossref request per DOI, so it is
    bounded by VERIFY_NETWORK_TOP_N (default 100 papers, by impact
    rank). OpenAlex lookups ARE batched (50 DOIs per request, using
    OpenAlex's documented `filter=doi:a|b|c` OR syntax) so they cover
    the whole set cheaply. Papers past the Crossref budget are scored
    offline and marked `authenticity_basis = 'offline_only'` — they are
    never presented as network-verified.

 5. The LLM decides nothing here. Every judgement in this file comes
    from structured metadata returned by Crossref / OpenAlex or from
    deterministic string comparison. The LLM's only contribution is the
    optional `deep_analysis` methodology text (from
    full_text_analysis.py), which contributes to the QUALITY score only,
    never to authenticity.
"""
import json
import os
import re
import math
import difflib
import time
import datetime as _dt
import requests
import threading
from concurrent.futures import ThreadPoolExecutor

from agents.textsim import tokenize, tfidf_vectors, cosine
import semantic_similarity
import venue_registry
import author_verification

TIMEOUT = 15
UA = {'User-Agent': 'ResearchLensAI/1.0 (mailto:megham112004@gmail.com)'}
CROSSREF_MAILTO = os.environ.get('CROSSREF_MAILTO', 'megham112004@gmail.com')

# How many papers (by impact rank) get the per-paper Crossref round trip.
VERIFY_NETWORK_TOP_N = int(os.environ.get('DEEP_VERIFY_NETWORK_TOP_N', '100'))
VERIFY_MAX_WORKERS = int(os.environ.get('DEEP_VERIFY_MAX_WORKERS', '6'))

# ── Polite HTTP for Crossref / OpenAlex ──────────────────────────────
# Verification fires many title searches in parallel; Crossref answers
# 429 Too Many Requests when that bursts. Cap concurrent requests per
# host and retry 429/5xx with backoff (honouring Retry-After).
_HOST_SEMAPHORES = {
    'crossref': threading.Semaphore(int(os.environ.get('CROSSREF_CONCURRENCY', '2'))),
    'openalex': threading.Semaphore(int(os.environ.get('OPENALEX_CONCURRENCY', '3'))),
}
# Minimum gap between request starts per host (seconds), and a circuit
# breaker: if a host still answers 429 after all retries, stop calling it
# for _COOLDOWN_S so the rest of the run isn't spent hammering it.
_MIN_GAP = {'crossref': float(os.environ.get('CROSSREF_MIN_GAP_S', '0.25')),
            'openalex': float(os.environ.get('OPENALEX_MIN_GAP_S', '0.15'))}
_COOLDOWN_S = float(os.environ.get('VERIFY_HOST_COOLDOWN_S', '90'))
_host_lock = threading.Lock()
_next_slot = {}       # host -> earliest time the next request may start
_blocked_until = {}   # host -> circuit-breaker deadline


def _pace(host_key):
    with _host_lock:
        now = time.time()
        start = max(now, _next_slot.get(host_key, 0.0))
        _next_slot[host_key] = start + _MIN_GAP[host_key]
    if start > now:
        time.sleep(start - now)


def _polite_get(host_key, url, params=None, retries=3):
    """requests.get with a per-host concurrency cap, pacing, 429/5xx retry
    and a circuit breaker. Returns the final response (caller still calls
    raise_for_status())."""
    if time.time() < _blocked_until.get(host_key, 0.0):
        raise RuntimeError(f"{host_key} rate-limited; skipped during cooldown")
    sem = _HOST_SEMAPHORES[host_key]
    resp = None
    for attempt in range(retries + 1):
        with sem:
            _pace(host_key)
            resp = requests.get(url, params=params, timeout=TIMEOUT, headers=UA)
        if resp.status_code not in (429, 500, 502, 503, 504):
            return resp
        if attempt == retries:
            break
        try:
            wait = float(resp.headers.get('Retry-After', ''))
        except ValueError:
            wait = 0.0
        time.sleep(min(10.0, max(wait, 1.5 * (2 ** attempt))))
    if resp is not None and resp.status_code == 429:
        _blocked_until[host_key] = time.time() + _COOLDOWN_S
        print(f"paper_verification: {host_key} keeps returning 429; pausing it for {_COOLDOWN_S:.0f}s")
    return resp


def _clean_search_title(title):
    """OpenAlex's `search` rejects some punctuation (a '?' in a title gives
    HTTP 400). Keep letters/digits only for the free-text query."""
    return re.sub(r'\s+', ' ', re.sub(r'[^\w\s]', ' ', title or '', flags=re.UNICODE)).strip()

# OpenAlex documents up to 50 values in one `filter=doi:a|b|c` OR query.
OPENALEX_BATCH = 50

AUTHENTICITY_REJECT_THRESHOLD = float(os.environ.get('DEEP_AUTHENTICITY_MIN', '75'))

# ─── The hard gate for RANKING (architecture-review item 4) ─────────
# Passing verification (not REJECTED/RETRACTED/OFF_TOPIC) is not the
# same claim as "trustworthy enough to shape the gap-analysis
# conclusions". UNVERIFIED papers are kept and shown — deleting them
# would hide exactly the "we could not check this" cases a reviewer
# most wants to see — but they must not silently vote in gap detection
# alongside a HIGHLY_VERIFIED paper. This is the status tier that is
# allowed to influence ranking/gap-detection by default; callers that
# want a looser or stricter cut pass their own set to
# filter_for_ranking() / verify_papers(ranking_statuses=...).
RANKING_STATUSES_DEFAULT = frozenset({'HIGHLY_VERIFIED', 'VERIFIED'})
# PARTIALLY_VERIFIED is shown as its own bucket but excluded from
# ranking by default -- widen via ranking_statuses= to include it.


def filter_for_ranking(papers, statuses=None):
    """Splits `papers` (already run through verify_papers) into
    (ranking_eligible, excluded). `statuses` defaults to
    RANKING_STATUSES_DEFAULT. Only the first list should ever reach
    gap detection, trend analysis, or full-text methodology extraction
    — the second list still belongs in the API response and the
    Verification Explorer (a reader may need to see exactly which
    papers were excluded and why), just not in the numbers that drive
    the pipeline's conclusions."""
    allowed = frozenset(statuses) if statuses else RANKING_STATUSES_DEFAULT
    eligible = [p for p in papers if p.get('verification_status') in allowed]
    excluded = [p for p in papers if p.get('verification_status') not in allowed]
    return eligible, excluded


# ─── Simple, directly-testable wrappers (architecture-review item 10) ──
# Thin, obviously-correct entry points a smoke test can call without
# needing to construct a full paper dict — exactly the shape the
# review's example assertions expect.

def validate_source(name):
    """True iff `name` is in the authorized scholarly-source registry.
    A bare domain or URL ('random-blog.com', 'https://x.com/paper') is
    never in AUTHORIZED_SOURCES, so this rejects exactly the case the
    review's smoke tests target: no arbitrary website can pass as a
    source, only names this codebase's own connectors actually use."""
    if not name:
        return False
    name = str(name)
    base = name.split(' (via ')[0].strip()
    via_dblp = '(via DBLP)' in name
    return _canonical_source(base) in _AUTHORIZED_LOWER or (via_dblp and 'dblp' in _AUTHORIZED_LOWER)


def valid_identifier(value, kind='doi'):
    """Format/checksum validation only — this does NOT hit the
    network or claim the identifier resolves to a real record (that is
    what identifier_resolves in compute_authenticity checks). kind='doi'
    checks the DOI prefix/shape (10.NNNN/suffix); kind='orcid' delegates
    to author_verification's real ISO 7064 check-digit validator."""
    if not value:
        return False
    if kind == 'orcid':
        import author_verification
        return author_verification.checksum_valid(value)
    if kind == 'doi':
        return bool(re.match(r'^10\.\d{4,9}/\S+$', str(value).strip()))
    raise ValueError(f"valid_identifier: unknown kind {kind!r}")
RELEVANCE_DISCARD_THRESHOLD = float(os.environ.get('DEEP_RELEVANCE_MIN', '35'))
# Opt-in (default OFF): reject on ONE severe field mismatch against a registry record that the paper's own
# identifier resolved to - a title that is almost unrelated to the registry's, or an author list sharing no
# name at all with it. Default OFF because a real paper with a translated title can trip the title rule;
# measure it with `python -m eval.run_eval --strict` before turning it on.
STRICT_FIELD_REJECT = os.environ.get('DEEP_STRICT_FIELD_REJECT', '0').lower() in ('1', 'true', 'yes')
SEVERE_TITLE_RATIO = float(os.environ.get('DEEP_SEVERE_TITLE_RATIO', '0.5'))
# Highest authenticity a paper can reach when NOTHING independent
# corroborated it (single source, no resolvable identifier). Chosen to
# sit below AUTHENTICITY_REJECT_THRESHOLD so such papers are always
# surfaced as unverified rather than silently ranked as verified.
UNCORROBORATED_CEILING = float(os.environ.get('DEEP_UNCORROBORATED_CEILING', '55'))

# String-similarity ratio above which two metadata values from different
# registries are treated as agreeing rather than contradicting.
FIELD_MATCH_RATIO = 0.90
TITLE_MATCH_RATIO = 0.88


# ─── Authenticity weight table ──────────────────────────────────────
# Mirrors the architecture note's 100-point table. Points for a check
# that COULD NOT RUN are removed from the denominator (renormalized),
# not awarded and not zero-filled.
AUTHENTICITY_POINTS = {
    'identifier_present': 25,
    'identifier_resolves': 15,
    'crossref_record_found': 20,
    'second_source_confirms': 15,
    'authors_match': 10,
    'title_match': 5,
    'year_match': 5,
    'venue_match': 5,
}

RELEVANCE_WEIGHTS = {
    'title_similarity': 0.25,
    'abstract_similarity': 0.30,
    'keyword_overlap': 0.20,
    'topic_overlap': 0.15,
    'semantic_similarity': 0.10,
    # Optional cross-encoder rerank (DEEP_CROSS_ENCODER=<model name>): 0 = off. When a model is configured
    # and loads, this weight defaults to 0.20 (set DEEP_RELEVANCE_WEIGHTS to change it).
    'cross_encoder': 0.0,
}

# Level-3 quality weights (the architecture note's 55% block,
# re-expressed as a 0-100 quality score of its own).
QUALITY_WEIGHTS = {
    'venue_quality': 15 / 55,
    'citation_impact': 10 / 55,
    'citation_recency': 8 / 55,
    'methodology': 10 / 55,
    'metadata_completeness': 4 / 55,
    'publication_type': 3 / 55,
    'retraction_status': 3 / 55,
    'author_affiliation': 2 / 55,
}

OVERALL_WEIGHTS = {'authenticity': 0.20, 'relevance': 0.25, 'quality': 0.55}


def _apply_weight_overrides():
    """DEEP_RELEVANCE_WEIGHTS / DEEP_QUALITY_WEIGHTS: JSON objects, e.g. the ones printed by
    `python -m eval.run_levels`. Unknown keys and negative values are ignored; a bad JSON string is
    reported and ignored (the shipped weights stay)."""
    for env, table in (('DEEP_RELEVANCE_WEIGHTS', RELEVANCE_WEIGHTS), ('DEEP_QUALITY_WEIGHTS', QUALITY_WEIGHTS)):
        raw = os.environ.get(env)
        if not raw:
            continue
        try:
            data = json.loads(raw)
            for k, v in data.items():
                if k in table and isinstance(v, (int, float)) and v >= 0:
                    table[k] = float(v)
        except (ValueError, AttributeError) as e:
            print(f'paper_verification: ignoring {env}: {e}')


_apply_weight_overrides()


# ─── Publication types ──────────────────────────────────────────────
# Canonical internal vocabulary. Mapped from Crossref `type`, OpenAlex
# `type`, Semantic Scholar `publicationTypes`, and source-of-origin
# facts (an arXiv record is a preprint unless another registry says it
# was subsequently published).
JOURNAL_ARTICLE = 'JOURNAL_ARTICLE'
CONFERENCE_PAPER = 'CONFERENCE_PAPER'
REVIEW = 'REVIEW'
SURVEY = 'SURVEY'
PREPRINT = 'PREPRINT'
THESIS = 'THESIS'
BOOK = 'BOOK'
BOOK_CHAPTER = 'BOOK_CHAPTER'
DATASET = 'DATASET'
EDITORIAL = 'EDITORIAL'
NEWS = 'NEWS'
OTHER = 'OTHER'
# Two extensions beyond the doc's 12-bucket ranking table (same
# rationale as SURVEY above it): the doc explicitly lists CaseReport
# and ClinicalTrial as real Semantic Scholar / PubMed publication
# types your system must recognize (section 9), but its own
# classify-into list and ranking table don't give them a bucket.
# Silently mapping either into JOURNAL_ARTICLE would score a single-
# patient case report identically to a full study, and would score a
# clinical trial (often the strongest evidence class in a biomedical
# search) the same as an ordinary journal article — both lose real
# information the source data actually provided. Kept as their own
# types instead.
CASE_REPORT = 'CASE_REPORT'
CLINICAL_TRIAL = 'CLINICAL_TRIAL'

# What the research-paper ranking allows through.
TYPE_POLICY = {
    JOURNAL_ARTICLE: ('allow', 1.00),
    CONFERENCE_PAPER: ('allow', 1.00),
    REVIEW: ('allow', 0.90),
    SURVEY: ('allow', 0.90),
    # A clinical trial report is primary evidence, often higher-rigor
    # than an ordinary observational journal article (randomization,
    # pre-registration) — not down-weighted.
    CLINICAL_TRIAL: ('allow', 1.00),
    # A case report is legitimate peer-reviewed research but describes
    # a single patient/instance — real evidence, deliberately weighted
    # below a full study rather than either excluded or treated as
    # equivalent to one.
    CASE_REPORT: ('allow', 0.70),
    PREPRINT: ('flag', 0.60),      # not peer reviewed — kept, marked, down-weighted
    THESIS: ('flag', 0.60),
    BOOK: ('allow', 0.80),
    BOOK_CHAPTER: ('allow', 0.75),
    OTHER: ('flag', 0.50),
    DATASET: ('exclude', 0.0),
    EDITORIAL: ('exclude', 0.0),
    NEWS: ('exclude', 0.0),
}

# Explicit, normalized mapping from every native publication-type
# string this pipeline actually sees (Semantic Scholar's CamelCase
# publicationTypes, PubMed's Title Case PublicationType list) to the
# canonical vocabulary above. Replaces fragile ad-hoc substring
# searches (e.g. checking whether the literal text "meta-analysis"
# happens to appear) with a real lookup table, so a type is recognized
# because its exact vocabulary term is in the table, not because a
# guessed substring happened to match. Keys are normalized to
# lowercase-letters-only so "Case Reports" (PubMed) and "CaseReport"
# (Semantic Scholar) both hit the same entry.
NATIVE_TYPE_MAP = {
    # Case report family
    'casereport': CASE_REPORT, 'casereports': CASE_REPORT,
    # Clinical trial family (PubMed's several trial-phase tags and
    # "Randomized Controlled Trial" all indicate the same real thing)
    'clinicaltrial': CLINICAL_TRIAL, 'clinicaltrialphasei': CLINICAL_TRIAL,
    'clinicaltrialphaseii': CLINICAL_TRIAL, 'clinicaltrialphaseiii': CLINICAL_TRIAL,
    'clinicaltrialphaseiv': CLINICAL_TRIAL, 'controlledclinicaltrial': CLINICAL_TRIAL,
    'randomizedcontrolledtrial': CLINICAL_TRIAL, 'pragmaticclinicaltrial': CLINICAL_TRIAL,
    'observationalstudy': CLINICAL_TRIAL, 'multicenterstudy': CLINICAL_TRIAL,
    # Review family — a meta-analysis is secondary literature synthesizing
    # other studies, the same family as a review, so it maps there rather
    # than getting its own bucket the doc's ranking table has no entry for.
    'review': REVIEW, 'metaanalysis': REVIEW, 'systematicreview': REVIEW,
    'scientificintegrityreview': REVIEW,
    # Editorial family
    'editorial': EDITORIAL, 'comment': EDITORIAL, 'letter': EDITORIAL,
    'paratext': EDITORIAL, 'publishederratum': EDITORIAL,
    # News
    'news': NEWS,
    # Dataset
    'dataset': DATASET,
    # Book
    'book': BOOK, 'booksection': BOOK_CHAPTER, 'bookchapter': BOOK_CHAPTER,
    # Plain journal/conference
    'journalarticle': JOURNAL_ARTICLE, 'conference': CONFERENCE_PAPER,
}

# When a record carries several native type tags at once (PubMed
# routinely does — a case report is tagged BOTH "Journal Article" and
# "Case Reports"), the more specific/informative type wins rather than
# whichever tag happened to be scanned first. Ordered most to least
# specific.
_NATIVE_TYPE_PRIORITY = (CASE_REPORT, CLINICAL_TRIAL, REVIEW, EDITORIAL, NEWS,
                         DATASET, BOOK_CHAPTER, BOOK, CONFERENCE_PAPER, JOURNAL_ARTICLE)


def _classify_from_native_types(native_list):
    """Real lookup, not a guess: normalizes every native type string in
    `native_list` and checks it against NATIVE_TYPE_MAP. Returns the
    highest-priority canonical type among the matches, or None if
    nothing in the list is recognized."""
    norm = lambda t: re.sub(r'[^a-z]', '', str(t).lower())
    matched = {NATIVE_TYPE_MAP[k] for k in (norm(t) for t in native_list if t) if k in NATIVE_TYPE_MAP}
    for candidate in _NATIVE_TYPE_PRIORITY:
        if candidate in matched:
            return candidate
    return None

_CROSSREF_TYPE_MAP = {
    'journal-article': JOURNAL_ARTICLE,
    'proceedings-article': CONFERENCE_PAPER,
    'posted-content': PREPRINT,
    'dissertation': THESIS,
    'book': BOOK,
    'monograph': BOOK,
    'edited-book': BOOK,
    'reference-book': BOOK,
    'book-chapter': BOOK_CHAPTER,
    'book-section': BOOK_CHAPTER,
    'book-part': BOOK_CHAPTER,
    'dataset': DATASET,
    'component': OTHER,
    'report': OTHER,
    'standard': OTHER,
    'peer-review': EDITORIAL,
    'journal-issue': EDITORIAL,
    'proceedings': OTHER,
}

_OPENALEX_TYPE_MAP = {
    'article': JOURNAL_ARTICLE,
    'journal-article': JOURNAL_ARTICLE,
    'proceedings-article': CONFERENCE_PAPER,
    'preprint': PREPRINT,
    'review': REVIEW,
    'dissertation': THESIS,
    'book': BOOK,
    'book-chapter': BOOK_CHAPTER,
    'dataset': DATASET,
    'editorial': EDITORIAL,
    'erratum': EDITORIAL,
    'letter': EDITORIAL,
    'paratext': EDITORIAL,
    'reference-entry': OTHER,
    'report': OTHER,
    'peer-review': EDITORIAL,
    'standard': OTHER,
    'supplementary-materials': OTHER,
}

_REVIEW_TITLE_RE = re.compile(
    r'\b(a |an |the )?(systematic |comprehensive |critical |brief |short )?'
    r'(review|survey|state[- ]of[- ]the[- ]art|meta[- ]analysis)\b', re.I)

# Preprint servers, identified from the venue/publisher string rather
# than assumed from the source name.
_PREPRINT_VENUE_RE = re.compile(r'\b(arxiv|biorxiv|medrxiv|chemrxiv|ssrn|preprints?\.org|research\s*square|osf preprints)\b', re.I)
_CONFERENCE_VENUE_RE = re.compile(r'\b(proceedings|conference|symposium|workshop|congress|meeting)\b', re.I)
_JOURNAL_VENUE_RE = re.compile(r'\b(journal|transactions|letters|review of|annals|bulletin|acta|quarterly)\b', re.I)

# DOAJ = vetted open-access journal index; DBLP strict venues already
# have a dedicated bonus in deep_collection.
_INDEXED_SOURCES = {'DOAJ', 'PubMed', 'Europe PMC', 'INSPIRE-HEP', 'IEEE Xplore',
                    'Springer Nature', 'ScienceDirect', 'NASA ADS'}
_STRICT_VENUES = ('AAAI', 'NeurIPS', 'ICML', 'ICLR')

METADATA_REQUIRED_FIELDS = ('title', 'authors', 'year', 'venue', 'identifier',
                            'abstract', 'publisher', 'publication_type')

# ─── Source trust check ─────────────────────────────────────────────
# The registry of sources this app is allowed to ingest from. Every
# entry is a source whose OFFICIAL API is called directly by
# academic_sources.py / app.py — nothing here is scraped from a web page
# and nothing arrives from a URL a user supplied. A paper whose `source`
# is not in this registry did not come through the authorized fetch
# path, which is a hard stop: it is rejected before any scoring runs.
AUTHORIZED_SOURCES = {
    'ArXiv', 'Semantic Scholar', 'OpenAlex', 'Crossref', 'PubMed', 'Europe PMC',
    'DOAJ', 'ERIC', 'Internet Archive', 'INSPIRE-HEP', 'IEEE Xplore',
    'Springer Nature', 'CORE', 'ScienceDirect', 'DBLP', 'NASA ADS',
}

# Source names are compared case-insensitively. The arXiv fetcher labels its
# papers 'arXiv' (the arXiv project's own spelling) while this registry says
# 'ArXiv', and an exact-case match made EVERY arXiv paper fail the source
# trust check and be rejected outright. Only the casing is relaxed: the name
# must still be one of the authorized connectors, so 'random-blog.com' or a
# URL is still rejected.
_AUTHORIZED_LOWER = {n.lower() for n in AUTHORIZED_SOURCES}


def _canonical_source(name):
    return (name or '').strip().lower()


def check_source_authorized(paper):
    """Sets `source_authorized` and `source_authorized_detail`. DBLP
    venue streams arrive labelled e.g. "AAAI (via DBLP)" — the base
    source name is what gets checked."""
    source = (paper.get('source') or '').strip()
    base = source.split(' (via ')[0].strip()
    via_dblp = '(via DBLP)' in source
    authorized = _canonical_source(base) in _AUTHORIZED_LOWER or (via_dblp and 'dblp' in _AUTHORIZED_LOWER)
    paper['source_authorized'] = authorized
    paper['source_authorized_detail'] = (
        f"'{source}' is in the authorized scholarly-source registry, fetched via its official API"
        if authorized else
        f"'{source}' is not in the authorized scholarly-source registry")
    return paper


# ─── Small helpers ──────────────────────────────────────────────────

def _norm_doi(doi):
    d = (doi if isinstance(doi, str) else str(doi or '')).strip().lower()
    d = re.sub(r'^https?://(dx\.)?doi\.org/', '', d)
    return d or None


def _norm_text(t):
    return re.sub(r'[^a-z0-9]+', ' ', (t or '').lower()).strip()


def _ratio(a, b):
    a, b = _norm_text(a), _norm_text(b)
    if not a or not b:
        return None
    return difflib.SequenceMatcher(None, a, b).ratio()


def _surnames(authors):
    out = set()
    for a in authors or []:
        a = (a or '').strip()
        if a:
            out.add(a.split()[-1].lower())
    return out


def _year_int(value):
    m = re.search(r'\b(1[6-9]\d{2}|20\d{2}|21\d{2})\b', str(value or ''))
    return int(m.group(1)) if m else None


def _name_tokens(authors):
    """Every alphabetic name token of length >= 3, so the comparison survives name order ("Wang Wei" vs
    "Wei Wang"), initials ("J. Smith" vs "John Smith") and abbreviated author lists."""
    out = set()
    for a in authors or []:
        if isinstance(a, str):
            out |= {t for t in re.findall(r"[^\W\d_]{3,}", a.lower())}
    return out


def _severe_mismatches(paper, registries):
    """Fields that are not merely different but unrelated to what a registry holds for this record:
    'title' (similarity below SEVERE_TITLE_RATIO) and 'authors' (>= 2 name tokens on our side, none shared
    with any registry's author list). [] when no registry record was found."""
    live = [r for r in registries if r and r.get('found')]
    if not live:
        return []
    out = []
    ratios = [x for x in (_ratio(paper.get('title'), r.get('title')) for r in live) if x is not None]
    if ratios and max(ratios) < SEVERE_TITLE_RATIO:
        out.append('title')
    ours = _name_tokens(paper.get('authors'))
    theirs = [t for t in (_name_tokens(r.get('authors')) for r in live) if t]
    if len(ours) >= 2 and theirs and all(not (ours & t) for t in theirs):
        out.append('authors')
    return out


def _paper_identifiers(paper):
    """Every persistent identifier actually present on this record."""
    ids = {}
    doi = _norm_doi(paper.get('doi'))
    if doi:
        ids['doi'] = doi
    for key, field in (('pmid', 'pmid'), ('pmcid', 'pmcid'), ('arxiv_id', 'arxiv_id'),
                       ('openalex_id', 'openalex_id'), ('ss_id', 'ss_id'),
                       ('core_id', 'core_id'), ('dblp_key', 'dblp_key'),
                       ('inspire_id', 'inspire_id'), ('isbn', 'isbn')):
        value = paper.get(field)
        if value:
            ids[key] = str(value)
    return ids


# ─── Network: Crossref and OpenAlex lookups ─────────────────────────

def crossref_lookup(doi):
    """GET https://api.crossref.org/works/{doi}. Returns a flat dict of
    the fields this module compares, or None when the DOI does not
    resolve at Crossref (404 -> a real, meaningful negative signal) or
    the request failed (-> `error`, which is NOT treated as a negative
    signal anywhere in the scoring)."""
    doi = _norm_doi(doi)
    if not doi:
        return None
    try:
        resp = requests.get(f"https://api.crossref.org/works/{requests.utils.quote(doi)}",
                            params={'mailto': CROSSREF_MAILTO}, timeout=TIMEOUT, headers=UA)
    except Exception as e:
        return {'error': str(e)}
    if resp.status_code == 404:
        return {'found': False}
    if resp.status_code != 200:
        return {'error': f'HTTP {resp.status_code}'}
    try:
        msg = resp.json().get('message') or {}
    except ValueError as e:
        return {'error': f'bad JSON: {e}'}

    title_list = msg.get('title') or []
    authors = []
    for a in (msg.get('author') or []):
        name = ' '.join(x for x in [a.get('given'), a.get('family')] if x)
        if name:
            authors.append(name)
    orcids = [a.get('ORCID') for a in (msg.get('author') or []) if a.get('ORCID')]
    affiliations = [aff.get('name') for a in (msg.get('author') or [])
                    for aff in (a.get('affiliation') or []) if aff.get('name')]
    year = None
    for key in ('published', 'published-print', 'published-online', 'issued'):
        parts = (msg.get(key) or {}).get('date-parts')
        if parts and parts[0] and parts[0][0]:
            year = int(parts[0][0])
            break
    # Crossref exposes corrections/retractions through `update-to`
    # (present on the NOTICE, pointing at what it corrects) and
    # `updated-by` (present on the corrected work). A retraction notice
    # pointing at this DOI is the real signal we want.
    updated_by = msg.get('updated-by') or []
    retracted = any((u.get('type') or '').lower() in
                    ('retraction', 'withdrawal', 'removal') for u in updated_by)
    is_notice = any((u.get('type') or '').lower() in
                    ('retraction', 'withdrawal', 'removal') for u in (msg.get('update-to') or []))
    return {
        'found': True,
        'title': (title_list[0] if title_list else ''),
        'authors': authors,
        'year': year,
        'venue': (msg.get('container-title') or [''])[0],
        'publisher': msg.get('publisher') or '',
        'type': (msg.get('type') or '').lower(),
        'issn': (msg.get('ISSN') or []),
        'isbn': (msg.get('ISBN') or []),
        'citation_count': msg.get('is-referenced-by-count'),
        'orcids': orcids,
        'affiliations': affiliations,
        'retracted': retracted,
        'is_retraction_notice': is_notice,
        'subject': msg.get('subject') or [],
    }


def _corroborates_candidate(paper, candidate_title, candidate_year, candidate_authors):
    """
    Shared safety gate for a metadata-based (no-DOI) match: requires a
    strong title ratio AND (year agreeing within 1 OR an author surname
    overlap) — the same two-signal discipline deep_collection.PaperDeduper
    uses for its own fuzzy-title tier, so a title-only near-miss (a
    numbered series, "Part I" vs "Part II") can never be accepted as
    the same paper on title alone.
    """
    title_ratio = _ratio(paper.get('title'), candidate_title)
    if title_ratio is None or title_ratio < TITLE_MATCH_RATIO:
        return False
    our_year = _year_int(paper.get('year'))
    cand_year = _year_int(candidate_year) if not isinstance(candidate_year, int) else candidate_year
    year_ok = our_year is not None and cand_year is not None and abs(our_year - cand_year) <= 1
    our_authors = _surnames(paper.get('authors'))
    cand_authors = _surnames(candidate_authors)
    authors_ok = bool(our_authors and cand_authors and (our_authors & cand_authors))
    return year_ok or authors_ok


def crossref_bibliographic_search(paper):
    """
    Genuine cross-source corroboration for a paper with NO DOI at all
    (DBLP, ERIC, Internet Archive, and any strict-venue paper the DBLP
    resolution check merely confirmed exists but did not corroborate
    against a second registry). Crossref's documented bibliographic
    free-text query (`query.bibliographic`) searches across title,
    author and other fields — used here strictly as a lookup, with the
    same title+year/author confirmation gate every other cross-source
    match in this module uses, never accepted on title similarity alone.

    Returns a dict in the same shape crossref_lookup() returns (so the
    rest of the module treats it identically), or None when nothing in
    Crossref's top results passes the corroboration gate.
    """
    title = (paper.get('title') or '').strip()
    if not title:
        return None
    try:
        resp = _polite_get(
            'crossref', "https://api.crossref.org/works",
            params={'query.bibliographic': title, 'rows': 3, 'mailto': CROSSREF_MAILTO})
        resp.raise_for_status()
        items = resp.json().get('message', {}).get('items', [])
    except Exception as e:
        print(f"paper_verification: Crossref bibliographic search failed: {e}")
        return {'error': str(e)}
    for item in items:
        cand_title = (item.get('title') or [''])[0]
        cand_year = None
        for key in ('published', 'published-print', 'published-online', 'issued'):
            parts = (item.get(key) or {}).get('date-parts')
            if parts and parts[0] and parts[0][0]:
                cand_year = parts[0][0]
                break
        cand_authors = [' '.join(x for x in [a.get('given'), a.get('family')] if x)
                        for a in (item.get('author') or [])]
        if _corroborates_candidate(paper, cand_title, cand_year, cand_authors):
            return {
                'found': True, 'title': cand_title, 'authors': cand_authors, 'year': cand_year,
                'venue': (item.get('container-title') or [''])[0],
                'publisher': item.get('publisher') or '',
                'type': (item.get('type') or '').lower(),
                'issn': item.get('ISSN') or [], 'isbn': item.get('ISBN') or [],
                'citation_count': item.get('is-referenced-by-count'),
                'orcids': [a.get('ORCID') for a in (item.get('author') or []) if a.get('ORCID')],
                'affiliations': [aff.get('name') for a in (item.get('author') or [])
                                for aff in (a.get('affiliation') or []) if aff.get('name')],
                'retracted': False, 'is_retraction_notice': False, 'subject': item.get('subject') or [],
                'matched_doi': item.get('DOI', ''),
                'match_basis': 'bibliographic_search',
            }
    return {'found': False}


def openalex_bibliographic_search(paper):
    """
    Same purpose as crossref_bibliographic_search but against OpenAlex's
    free-text `search` parameter — an independent index from Crossref
    (OpenAlex ingests many non-Crossref-registered records: theses,
    reports, some conference proceedings), so a paper that fails the
    Crossref search can still be genuinely corroborated here. Same
    two-signal confirmation gate; never accepted on title alone.
    """
    title = (paper.get('title') or '').strip()
    if not title:
        return None
    try:
        resp = _polite_get(
            'openalex', "https://api.openalex.org/works",
            params={'search': _clean_search_title(title) or title, 'per_page': 3,
                    'mailto': CROSSREF_MAILTO})
        resp.raise_for_status()
        results = resp.json().get('results', [])
    except Exception as e:
        print(f"paper_verification: OpenAlex bibliographic search failed: {e}")
        return {'error': str(e)}
    for item in results:
        cand_title = item.get('title') or item.get('display_name') or ''
        cand_authors = [a.get('author', {}).get('display_name', '')
                        for a in (item.get('authorships') or [])]
        if _corroborates_candidate(paper, cand_title, item.get('publication_year'), cand_authors):
            source = (item.get('primary_location') or {}).get('source') or {}
            return {
                'found': True, 'title': cand_title, 'authors': cand_authors,
                'year': item.get('publication_year'),
                'venue': source.get('display_name') or '',
                'publisher': source.get('host_organization_name') or '',
                'issn': source.get('issn') or [], 'type': (item.get('type') or '').lower(),
                'retracted': bool(item.get('is_retracted')), 'is_paratext': bool(item.get('is_paratext')),
                'citation_count': item.get('cited_by_count'),
                'openalex_id': (item.get('id') or '').replace('https://openalex.org/', ''),
                'source_type': (source.get('type') or '').lower(),
                'in_doaj': bool(source.get('is_in_doaj')),
                'orcids': [a.get('author', {}).get('orcid') for a in (item.get('authorships') or [])
                          if a.get('author', {}).get('orcid')],
                'affiliations': [inst.get('display_name') for a in (item.get('authorships') or [])
                                for inst in (a.get('institutions') or []) if inst.get('display_name')],
                'matched_doi': _norm_doi(item.get('doi')),
                'match_basis': 'bibliographic_search',
            }
    return {'found': False}


def openalex_lookup_batch(dois):
    """Batched OpenAlex works lookup using OpenAlex's documented OR
    filter syntax (`filter=doi:a|b|c`, up to 50 values per request).
    Returns {normalized_doi: {...}} for the DOIs OpenAlex knows; DOIs it
    has never seen are simply absent from the result — that is treated
    as "no OpenAlex corroboration", never as "fake"."""
    out = {}
    dois = [d for d in {_norm_doi(d) for d in dois} if d]
    for i in range(0, len(dois), OPENALEX_BATCH):
        chunk = dois[i:i + OPENALEX_BATCH]
        filter_value = '|'.join(f"https://doi.org/{d}" for d in chunk)
        try:
            resp = requests.get(
                'https://api.openalex.org/works',
                params={'filter': f'doi:{filter_value}', 'per_page': OPENALEX_BATCH,
                        'mailto': CROSSREF_MAILTO},
                timeout=TIMEOUT, headers=UA)
            resp.raise_for_status()
            results = resp.json().get('results') or []
        except Exception as e:
            print(f"paper_verification: OpenAlex batch lookup failed: {e}")
            continue
        for item in results:
            doi = _norm_doi(item.get('doi'))
            if not doi:
                continue
            source = (item.get('primary_location') or {}).get('source') or {}
            authorships = item.get('authorships') or []
            out[doi] = {
                'found': True,
                'title': item.get('title') or item.get('display_name') or '',
                'authors': [a.get('author', {}).get('display_name', '') for a in authorships],
                'orcids': [a.get('author', {}).get('orcid') for a in authorships
                           if a.get('author', {}).get('orcid')],
                'affiliations': [inst.get('display_name') for a in authorships
                                 for inst in (a.get('institutions') or []) if inst.get('display_name')],
                'year': item.get('publication_year'),
                'venue': source.get('display_name') or '',
                'publisher': source.get('host_organization_name') or '',
                'issn': source.get('issn') or [],
                'type': (item.get('type') or '').lower(),
                # OpenAlex exposes a real per-work boolean for this.
                'retracted': bool(item.get('is_retracted')),
                'is_paratext': bool(item.get('is_paratext')),
                'citation_count': item.get('cited_by_count'),
                'openalex_id': (item.get('id') or '').replace('https://openalex.org/', ''),
                'source_type': (source.get('type') or '').lower(),
                'in_doaj': bool(source.get('is_in_doaj')),
            }
    return out


SS_BATCH_URL = "https://api.semanticscholar.org/graph/v1/paper/batch"
SS_BATCH_SIZE = 500          # Semantic Scholar's documented max ids per batch call
SS_BATCH_FIELDS = ("title,year,venue,authors,authors.externalIds,externalIds,"
                   "publicationTypes,publicationVenue,citationCount,"
                   "influentialCitationCount,isOpenAccess")
SS_BATCH_DELAY = 1.0
SS_RETRIES = int(os.environ.get('S2_RETRIES', '3'))          # extra tries after an HTTP 429
SS_MAX_WAIT = float(os.environ.get('S2_MAX_WAIT_S', '20'))   # cap on one backoff sleep


def semantic_scholar_lookup_batch(papers):
    """
    Third independent registry. Uses Semantic Scholar's documented
    /paper/batch endpoint (the same one deep_collection already uses for
    citation backfill), keyed on DOI or arXiv ID, so a paper can be
    corroborated even when it has no DOI — which is exactly the arXiv /
    DBLP case that Crossref can never check.

    Returns {external_id: {...}}. An ID Semantic Scholar does not
    recognize is simply absent: no corroboration, never a fabricated
    record. The unkeyed tier is rate-limited, so a failed batch is
    logged and skipped rather than raised — those papers fall back to
    whatever corroboration Crossref/OpenAlex provided.
    """
    id_map = {}
    for paper in papers:
        doi = _norm_doi(paper.get('doi'))
        arxiv_id = (paper.get('arxiv_id') or '').strip()
        if doi:
            id_map[f"DOI:{doi}"] = None
        elif arxiv_id:
            id_map[f"ARXIV:{arxiv_id}"] = None
    ids = list(id_map.keys())
    out = {}
    # Which IDs were actually ASKED about and got an answer. Without
    # this, a rate-limited or failed batch is indistinguishable from
    # "Semantic Scholar does not recognize this ID" — and since the
    # arXiv branch of the authenticity ladder treats an unrecognized
    # arXiv ID as a FAILED identifier resolution (which rejects the
    # paper outright), an S2 outage would silently reject most arXiv
    # papers in the run. Recorded under a reserved key the caller
    # pops off; it is never confused with a paper entry because no
    # real external ID has this shape.
    answered = set()
    for i in range(0, len(ids), SS_BATCH_SIZE):
        chunk = ids[i:i + SS_BATCH_SIZE]
        try:
            headers = dict(UA)
            if os.environ.get('S2_API_KEY'):
                headers['x-api-key'] = os.environ['S2_API_KEY']      # keyed tier: far fewer 429s
            for attempt in range(SS_RETRIES + 1):
                resp = requests.post(SS_BATCH_URL, params={'fields': SS_BATCH_FIELDS},
                                     json={'ids': chunk}, timeout=30, headers=headers)
                if resp.status_code != 429 or attempt == SS_RETRIES:
                    break
                try:
                    wait = float(resp.headers.get('Retry-After') or 0)
                except (TypeError, ValueError):
                    wait = 0.0
                time.sleep(min(SS_MAX_WAIT, max(wait, 2.0 * (attempt + 1))))
            resp.raise_for_status()
            results = resp.json()
        except Exception as e:
            print(f"paper_verification: Semantic Scholar batch lookup failed: {e}")
            continue
        answered.update(chunk)
        for ext_id, item in zip(chunk, results):
            if not item:
                continue
            venue_record = item.get('publicationVenue') or {}
            out[ext_id] = {
                'found': True,
                'title': item.get('title') or '',
                'authors': [a.get('name', '') for a in (item.get('authors') or [])],
                # Real ORCID, when S2 has one on file for an author —
                # requested via authors.externalIds above. S2 does not
                # expose author affiliations on this endpoint (no
                # fabricated substitute), so affiliations stays [].
                'orcids': [a.get('externalIds', {}).get('ORCID')
                          for a in (item.get('authors') or [])
                          if (a.get('externalIds') or {}).get('ORCID')],
                'affiliations': [],
                'paper_id': item.get('paperId', ''),
                'year': item.get('year'),
                'venue': item.get('venue') or venue_record.get('name') or '',
                'publisher': '',    # S2 is an index, not a publisher of record
                'issn': venue_record.get('issn') and [venue_record['issn']] or [],
                'type': '',
                'publication_types': item.get('publicationTypes') or [],
                'retracted': None,  # S2 exposes no retraction flag on this endpoint
                'citation_count': item.get('citationCount'),
                'influential_citation_count': item.get('influentialCitationCount'),
                'is_open_access': item.get('isOpenAccess'),
                'venue_type': (venue_record.get('type') or '').lower(),
            }
        if i + SS_BATCH_SIZE < len(ids):
            time.sleep(SS_BATCH_DELAY)
    out['__answered__'] = answered
    return out


def pubmed_lookup(pmid):
    """
    Real resolution + metadata pull for a PMID, via NCBI's documented
    ESummary endpoint (no API key required; rate-limited to ~3 req/sec
    without one, which is why this is called only within the bounded
    network-verification budget, same as Crossref). Returns a dict in
    the shared registry shape (found/title/authors/year/venue/...), or
    {'found': False} for an ESummary "error" result (a PMID NCBI does
    not recognize — a real, meaningful negative signal, same as a 404
    DOI at Crossref), or {'error': ...} for a request failure (never
    treated as a negative signal).
    """
    pmid = str(pmid or '').strip()
    if not pmid or not pmid.isdigit():
        return None
    try:
        resp = requests.get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
            params={'db': 'pubmed', 'id': pmid, 'retmode': 'json',
                   'tool': 'ResearchLensAI', 'email': CROSSREF_MAILTO},
            timeout=TIMEOUT, headers=UA)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        return {'error': str(e)}
    result = payload.get('result', {})
    doc = result.get(pmid)
    if not doc or doc.get('error') or pmid not in result.get('uids', [pmid]):
        return {'found': False}
    year_match = re.search(r'\b(1[6-9]\d{2}|20\d{2})\b', doc.get('pubdate') or '')
    return {
        'found': True,
        'title': doc.get('title') or '',
        'authors': [a.get('name', '') for a in (doc.get('authors') or [])],
        'year': int(year_match.group(1)) if year_match else None,
        'venue': doc.get('fulljournalname') or doc.get('source') or '',
        'publisher': '',   # PubMed is an index, not a publisher of record
        'issn': [doc.get('issn')] if doc.get('issn') else [],
        'type': '',
        'publication_types': doc.get('pubtype') or [],
        'retracted': any('retract' in str(t).lower() for t in (doc.get('pubtype') or [])),
        'orcids': [], 'affiliations': [],
        'citation_count': None,
    }


def openalex_id_lookup(openalex_id):
    """Real single-work resolution against OpenAlex's own record store —
    distinct from openalex_lookup_batch (which is keyed by DOI). Confirms
    an OpenAlex ID this paper carries actually points to a live record."""
    oaid = re.sub(r'^https?://openalex\.org/', '', str(openalex_id or '').strip())
    if not oaid:
        return None
    try:
        resp = requests.get(f"https://api.openalex.org/works/{oaid}",
                            params={'mailto': CROSSREF_MAILTO}, timeout=TIMEOUT, headers=UA)
    except Exception as e:
        return {'error': str(e)}
    if resp.status_code == 404:
        return {'found': False}
    if resp.status_code != 200:
        return {'error': f'HTTP {resp.status_code}'}
    item = resp.json()
    source = (item.get('primary_location') or {}).get('source') or {}
    authorships = item.get('authorships') or []
    return {
        'found': True, 'title': item.get('title') or item.get('display_name') or '',
        'authors': [a.get('author', {}).get('display_name', '') for a in authorships],
        'orcids': [a.get('author', {}).get('orcid') for a in authorships
                  if a.get('author', {}).get('orcid')],
        'affiliations': [inst.get('display_name') for a in authorships
                         for inst in (a.get('institutions') or []) if inst.get('display_name')],
        'year': item.get('publication_year'), 'venue': source.get('display_name') or '',
        'publisher': source.get('host_organization_name') or '',
        'issn': source.get('issn') or [], 'type': (item.get('type') or '').lower(),
        'retracted': bool(item.get('is_retracted')), 'citation_count': item.get('cited_by_count'),
        'source_type': (source.get('type') or '').lower(), 'in_doaj': bool(source.get('is_in_doaj')),
    }


def semantic_scholar_id_lookup(ss_id):
    """Real single-paper resolution against S2's own record store —
    distinct from the batch DOI/arXiv-keyed lookup. Confirms an S2 paper
    ID this record carries (e.g. backfilled from an earlier corroboration)
    still resolves."""
    ss_id = str(ss_id or '').strip()
    if not ss_id:
        return None
    try:
        resp = requests.get(f"https://api.semanticscholar.org/graph/v1/paper/{ss_id}",
                            params={'fields': SS_BATCH_FIELDS}, timeout=TIMEOUT, headers=UA)
    except Exception as e:
        return {'error': str(e)}
    if resp.status_code == 404:
        return {'found': False}
    if resp.status_code != 200:
        return {'error': f'HTTP {resp.status_code}'}
    item = resp.json()
    venue_record = item.get('publicationVenue') or {}
    return {
        'found': True, 'title': item.get('title') or '',
        'authors': [a.get('name', '') for a in (item.get('authors') or [])],
        'orcids': [a.get('externalIds', {}).get('ORCID') for a in (item.get('authors') or [])
                  if (a.get('externalIds') or {}).get('ORCID')],
        'affiliations': [], 'year': item.get('year'),
        'venue': item.get('venue') or venue_record.get('name') or '', 'publisher': '',
        'issn': [venue_record['issn']] if venue_record.get('issn') else [], 'type': '',
        'publication_types': item.get('publicationTypes') or [], 'retracted': None,
        'citation_count': item.get('citationCount'),
        'influential_citation_count': item.get('influentialCitationCount'),
    }


def core_id_lookup(core_id):
    """Real single-work resolution against CORE's own record store.
    Requires CORE_API_KEY (already used elsewhere in this app for CORE
    search) — returns None (not applicable, not a failure) when no key
    is configured, exactly like every other optional-key source in
    academic_sources.py."""
    api_key = os.environ.get('CORE_API_KEY')
    core_id = str(core_id or '').strip()
    if not api_key or not core_id:
        return None
    try:
        resp = requests.get(f"https://api.core.ac.uk/v3/works/{core_id}",
                            headers={**UA, 'Authorization': f'Bearer {api_key}'}, timeout=TIMEOUT)
    except Exception as e:
        return {'error': str(e)}
    if resp.status_code == 404:
        return {'found': False}
    if resp.status_code != 200:
        return {'error': f'HTTP {resp.status_code}'}
    item = resp.json()
    return {
        'found': True, 'title': item.get('title') or '',
        'authors': [a.get('name', '') for a in (item.get('authors') or [])],
        'year': item.get('yearPublished'), 'venue': (item.get('journals') or [{}])[0].get('title', '')
        if item.get('journals') else (item.get('publisher') or ''),
        'publisher': item.get('publisher') or '', 'issn': [], 'type': item.get('documentType') or '',
        'retracted': None, 'orcids': [], 'affiliations': [], 'citation_count': None,
    }


def inspire_id_lookup(inspire_id):
    """Real single-record resolution against INSPIRE-HEP's own API —
    the same record ID already captured at fetch time in
    academic_sources.fetch_inspire_hep."""
    inspire_id = str(inspire_id or '').strip()
    if not inspire_id:
        return None
    try:
        resp = requests.get(f"https://inspirehep.net/api/literature/{inspire_id}",
                            timeout=TIMEOUT, headers=UA)
    except Exception as e:
        return {'error': str(e)}
    if resp.status_code == 404:
        return {'found': False}
    if resp.status_code != 200:
        return {'error': f'HTTP {resp.status_code}'}
    meta = (resp.json().get('metadata') or {})
    titles = meta.get('titles') or []
    year = None
    pub_info = meta.get('publication_info') or []
    if pub_info and pub_info[0].get('year'):
        year = pub_info[0]['year']
    return {
        'found': True, 'title': (titles[0].get('title') if titles else '') or '',
        'authors': [a.get('full_name', '') for a in (meta.get('authors') or [])],
        'year': year, 'venue': (pub_info[0].get('journal_title', '') if pub_info else ''),
        'publisher': '', 'issn': [], 'type': '', 'retracted': None,
        'orcids': [], 'affiliations': [], 'citation_count': meta.get('citation_count'),
    }


def isbn_resolves(isbn):
    """Real resolution check for an ISBN via the Open Library API — no
    key required. Confirms the ISBN this record carries actually
    identifies a real published work; does not attempt to compare
    metadata (Open Library's book-level records don't align cleanly
    with a paper/chapter's own title), so this feeds identifier_resolves
    only, not field-level comparison."""
    isbn = re.sub(r'[^0-9Xx]', '', str(isbn or ''))
    if not isbn:
        return None
    try:
        resp = requests.get(f"https://openlibrary.org/isbn/{isbn}.json",
                            timeout=TIMEOUT, headers=UA, allow_redirects=True)
    except Exception as e:
        print(f"paper_verification: Open Library ISBN check failed: {e}")
        return None
    if resp.status_code == 200:
        return True
    if resp.status_code == 404:
        return False
    return None


def dblp_resolves(key):
    """
    Real network check: does DBLP still have a record at this key?
    DBLP publishes each publication's own XML record at a documented,
    predictable URL (https://dblp.org/rec/{key}.xml) — a 200 confirms
    the key is genuine and current; a 404 means it does not (moved,
    merged into another entry, or fabricated). This is the same kind
    of "does the identifier actually resolve" check DOI resolution
    against Crossref performs, just against DBLP's own record store.

    Returns True / False / None (None = the request itself failed —
    DBLP unreachable, never treated as "did not resolve").
    """
    key = (key or '').strip().strip('/')
    if not key:
        return None
    try:
        resp = requests.head(f"https://dblp.org/rec/{requests.utils.quote(key)}.xml",
                             timeout=TIMEOUT, headers=UA, allow_redirects=True)
    except Exception as e:
        print(f"paper_verification: DBLP resolution check failed for {key}: {e}")
        return None
    if resp.status_code == 200:
        return True
    if resp.status_code == 404:
        return False
    return None  # anything else (5xx, rate limit) is inconclusive, not a failure


def _ss_key(paper):
    doi = _norm_doi(paper.get('doi'))
    if doi:
        return f"DOI:{doi}"
    arxiv_id = (paper.get('arxiv_id') or '').strip()
    return f"ARXIV:{arxiv_id}" if arxiv_id else None


# ─── Level 1: authenticity ──────────────────────────────────────────

def _compare_fields(paper, registries):
    """Deterministic field-by-field agreement across independent
    registries. Returns {field: True/False/None} — None means the
    comparison could not be made (one side had no value), which is
    never counted as agreement OR as contradiction."""
    verdict = {'title': None, 'authors': None, 'year': None, 'venue': None, 'publisher': None}
    live = [r for r in registries if r and r.get('found')]
    if not live:
        return verdict

    ratios = [_ratio(paper.get('title'), r.get('title')) for r in live]
    ratios = [x for x in ratios if x is not None]
    if ratios:
        verdict['title'] = max(ratios) >= TITLE_MATCH_RATIO

    ours = _surnames(paper.get('authors'))
    if ours:
        theirs_sets = [_surnames(r.get('authors')) for r in live]
        theirs_sets = [s for s in theirs_sets if s]
        if theirs_sets:
            verdict['authors'] = any(bool(ours & s) for s in theirs_sets)

    our_year = _year_int(paper.get('year'))
    if our_year:
        their_years = [r.get('year') for r in live if r.get('year')]
        if their_years:
            # Registries legitimately differ by a year (online-first vs
            # issue date), so ±1 is agreement, anything wider is not.
            verdict['year'] = any(abs(our_year - int(y)) <= 1 for y in their_years)

    if (paper.get('venue') or '').strip():
        venue_ratios = [_ratio(paper.get('venue'), r.get('venue')) for r in live]
        venue_ratios = [x for x in venue_ratios if x is not None]
        if venue_ratios:
            verdict['venue'] = max(venue_ratios) >= FIELD_MATCH_RATIO

    publishers = [r.get('publisher') for r in live if (r.get('publisher') or '').strip()]
    if len(publishers) >= 2:
        verdict['publisher'] = (_ratio(publishers[0], publishers[1]) or 0) >= FIELD_MATCH_RATIO
    return verdict


def compute_authenticity(paper, crossref=None, openalex=None, s2=None, s2_answered=True,
                         dblp_resolved=None, crossref_meta=None, openalex_meta=None,
                         pmid_result=None, openalex_id_result=None, ss_id_result=None,
                         core_id_result=None, inspire_id_result=None, isbn_resolved=None,
                         network_ran=True):
    """
    Level 1. Sets `authenticity_score` (0-100), `authenticity_checks`
    (every check with pass / fail / not-applicable and the points it
    carried), `authenticity_basis`, `authenticity_checked_weight_pct`
    (what fraction of the full 100-point table could actually be
    evaluated for this paper) and `verified_publisher`.

    `crossref` / `openalex` are the DOI-keyed lookup results for THIS
    paper, or None when no lookup was possible (no DOI) or none was
    attempted (outside the network budget — pass network_ran=False for
    that case, which keeps a skipped check out of the denominator
    instead of scoring it as a failure).

    Every OTHER identifier type this record might carry gets its own
    real resolution check, tried in priority order when a DOI is
    absent: `dblp_resolved` (dblp_resolves()), `pmid_result`
    (pubmed_lookup()), `openalex_id_result` (openalex_id_lookup()),
    `ss_id_result` (semantic_scholar_id_lookup()), `core_id_result`
    (core_id_lookup()), `inspire_id_result` (inspire_id_lookup()),
    `isbn_resolved` (isbn_resolves() — True/False/None only, no
    metadata). `crossref_meta` / `openalex_meta` are
    crossref_bibliographic_search() / openalex_bibliographic_search()
    results — genuine cross-source corroboration for a paper with no
    DOI, tried regardless of which other identifier resolved.

    Every lookup result that came back `found` (from any identifier
    type) is pooled into the SAME corroboration and field-comparison
    logic — a paper corroborated via its OpenAlex ID and its PMID has
    two independent confirmations, exactly as if it had been confirmed
    via two DOI-keyed registries.
    """
    checks = {}
    ids = _paper_identifiers(paper)
    also_indexed = [s for s in (paper.get('also_indexed_by') or []) if s]

    def record(name, passed, detail=None):
        checks[name] = {'result': passed, 'points': AUTHENTICITY_POINTS[name], 'detail': detail}

    # --- identifier present
    record('identifier_present', bool(ids), detail=sorted(ids.keys()) or None)

    cr_found = bool(crossref and crossref.get('found'))
    cr_missing = bool(crossref and crossref.get('found') is False)
    cr_errored = bool(crossref and crossref.get('error'))
    oa_found = bool(openalex and openalex.get('found'))
    ss_found = bool(s2 and s2.get('found'))
    # Bibliographic-search matches count as real corroboration (they
    # already passed _corroborates_candidate's title+year/author gate)
    # but never as "the DOI resolves" — they found an independent
    # record, they did not resolve THIS paper's own identifier.
    crm_found = bool(crossref_meta and crossref_meta.get('found'))
    oam_found = bool(openalex_meta and openalex_meta.get('found'))
    # Every other identifier type's own single-record resolution check.
    pmid_found = bool(pmid_result and pmid_result.get('found'))
    oaid_found = bool(openalex_id_result and openalex_id_result.get('found'))
    ssid_found = bool(ss_id_result and ss_id_result.get('found'))
    core_found = bool(core_id_result and core_id_result.get('found'))
    inspire_found = bool(inspire_id_result and inspire_id_result.get('found'))

    # --- identifier resolves / crossref record
    # Priority ladder when no DOI is present: each branch is a REAL
    # network resolution check against that identifier's own registry,
    # tried in the order the identifier is likely to be authoritative
    # for this app's sources (arXiv/PubMed/DBLP cover the large
    # majority of no-DOI papers this pipeline actually collects).
    if not ids.get('doi'):
        record('crossref_record_found', None, 'no DOI on record (Crossref is DOI-only)')
        if ids.get('arxiv_id') and network_ran and (ss_found or s2_answered):
            # Only a batch that ACTUALLY ANSWERED can produce a False
            # here. If Semantic Scholar was unreachable or rate-limited,
            # this drops through to the not-checked branch below instead
            # of declaring the arXiv ID unresolvable.
            record('identifier_resolves', bool(ss_found),
                   'arXiv ID resolved at Semantic Scholar' if ss_found
                   else 'arXiv ID not recognized by Semantic Scholar')
        elif ids.get('arxiv_id') and network_ran:
            record('identifier_resolves', None,
                   'Semantic Scholar unreachable or rate-limited — arXiv ID not checked '
                   '(absence of an answer is not evidence the ID is fake)')
        elif ids.get('pmid') and network_ran and pmid_result is not None and 'error' not in pmid_result:
            record('identifier_resolves', pmid_found,
                   'PMID resolved at NCBI (ESummary)' if pmid_found
                   else 'PMID not recognized by NCBI (ESummary)')
        elif ids.get('dblp_key') and network_ran and dblp_resolved is not None:
            record('identifier_resolves', dblp_resolved,
                   'DBLP key resolves at dblp.org' if dblp_resolved
                   else 'DBLP key did not resolve at dblp.org (404)')
        elif (ids.get('openalex_id') and network_ran and openalex_id_result is not None
              and 'error' not in openalex_id_result):
            record('identifier_resolves', oaid_found,
                   'OpenAlex ID resolves at openalex.org' if oaid_found
                   else 'OpenAlex ID did not resolve (404)')
        elif ids.get('ss_id') and network_ran and ss_id_result is not None and 'error' not in ss_id_result:
            record('identifier_resolves', ssid_found,
                   'Semantic Scholar paper ID resolves' if ssid_found
                   else 'Semantic Scholar paper ID did not resolve (404)')
        elif ids.get('core_id') and network_ran and core_id_result is not None and 'error' not in core_id_result:
            record('identifier_resolves', core_found,
                   'CORE ID resolves at core.ac.uk' if core_found
                   else 'CORE ID did not resolve (404)')
        elif (ids.get('inspire_id') and network_ran and inspire_id_result is not None
              and 'error' not in inspire_id_result):
            record('identifier_resolves', inspire_found,
                   'INSPIRE-HEP record ID resolves' if inspire_found
                   else 'INSPIRE-HEP record ID did not resolve (404)')
        elif ids.get('isbn') and network_ran and isbn_resolved is not None:
            record('identifier_resolves', isbn_resolved,
                   'ISBN resolves at Open Library' if isbn_resolved
                   else 'ISBN did not resolve at Open Library (404)')
        else:
            # Either this record has no identifier at all, or the one(s)
            # it has fell outside the network budget this run. Excluded
            # from the denominator rather than scored as a failure.
            record('identifier_resolves', None, 'no resolvable identifier checked for this record')
    elif not network_ran:
        record('identifier_resolves', None, 'outside network-verification budget')
        record('crossref_record_found', None, 'outside network-verification budget')
    elif cr_errored:
        record('identifier_resolves', None, f"Crossref unreachable: {crossref.get('error')}")
        record('crossref_record_found', None, 'Crossref unreachable')
    elif cr_found:
        record('identifier_resolves', True, 'DOI resolves at Crossref')
        record('crossref_record_found', True, 'Crossref record retrieved')
    elif cr_missing and (oa_found or ss_found):
        # DOI unknown to Crossref but known to another registry — happens
        # for DataCite-registered DOIs (arXiv, Zenodo). Real, not fake.
        resolver = 'OpenAlex' if oa_found else 'Semantic Scholar'
        record('identifier_resolves', True, f'DOI unknown to Crossref but resolved via {resolver}')
        record('crossref_record_found', False, 'not a Crossref-registered DOI')
    else:
        record('identifier_resolves', False,
               'DOI did not resolve at Crossref, OpenAlex or Semantic Scholar')
        record('crossref_record_found', False, 'no Crossref record for this DOI')

    # --- second independent source
    # Pool every registry that came back `found`, regardless of which
    # identifier type led there — a paper corroborated via PMID and one
    # corroborated via DOI are equally corroborated.
    all_registries = [crossref, openalex, s2, crossref_meta, openalex_meta,
                      pmid_result, openalex_id_result, ss_id_result,
                      core_id_result, inspire_id_result]
    found_flags = [cr_found, oa_found, ss_found, crm_found, oam_found,
                  pmid_found, oaid_found, ssid_found, core_found, inspire_found]
    found_labels = ['Crossref (DOI lookup)', 'OpenAlex (DOI lookup)', 'Semantic Scholar (batch lookup)',
                   'Crossref (bibliographic search)', 'OpenAlex (bibliographic search)',
                   'PubMed (PMID lookup)', 'OpenAlex (ID lookup)', 'Semantic Scholar (ID lookup)',
                   'CORE (ID lookup)', 'INSPIRE-HEP (ID lookup)']

    corroborators = sorted(set(also_indexed))
    for flag, label in zip(found_flags, found_labels):
        if flag and label not in corroborators:
            corroborators.append(label)
    independent = len(set(also_indexed)) >= 2 or any(found_flags)
    if independent:
        record('second_source_confirms', True, corroborators)
    elif not network_ran and not also_indexed:
        record('second_source_confirms', None, 'no corroboration attempted')
    else:
        record('second_source_confirms', False, 'only one source indexed this record')

    # --- field-level agreement, pooled across every registry that
    # actually returned a record for this paper.
    field_verdict = _compare_fields(paper, all_registries)
    record('title_match', field_verdict['title'])
    record('authors_match', field_verdict['authors'])
    record('year_match', field_verdict['year'])
    record('venue_match', field_verdict['venue'])

    earned = sum(c['points'] for c in checks.values() if c['result'] is True)
    checkable = sum(c['points'] for c in checks.values() if c['result'] is not None)
    score = (earned / checkable * 100) if checkable else 0.0

    # Hard contradictions are not a scoring matter — they are a verdict.
    contradictions = [k for k in ('title_match', 'authors_match', 'year_match')
                      if checks[k]['result'] is False]
    identifier_failed = checks['identifier_resolves']['result'] is False

    corroborated = checks['second_source_confirms']['result'] is True or any(found_flags)
    if not corroborated:
        score = min(score, UNCORROBORATED_CEILING)
        basis = 'uncorroborated_single_source'
    elif not network_ran:
        basis = 'offline_only'
    elif cr_found or oa_found or ss_found or pmid_found or oaid_found or ssid_found or core_found or inspire_found:
        basis = 'registry_verified'
    elif crm_found or oam_found:
        basis = 'bibliographic_match_verified'
    else:
        basis = 'cross_source_only'
    paper['corroborating_registries'] = corroborators

    paper['authenticity_score'] = round(score, 2)
    paper['authenticity_checks'] = checks
    paper['authenticity_basis'] = basis
    paper['authenticity_checked_weight_pct'] = round(checkable, 1)
    paper['authenticity_contradictions'] = contradictions
    paper['authenticity_severe_mismatches'] = _severe_mismatches(paper, all_registries)
    # Generalized beyond DOI now that this check also covers arXiv IDs
    # (via Semantic Scholar), DBLP keys, PMIDs, OpenAlex IDs, S2 IDs,
    # CORE IDs and INSPIRE IDs — the verdict text names whichever
    # identifier actually failed.
    paper['authenticity_identifier_unresolved'] = identifier_failed
    _id_type_priority = ('doi', 'arxiv_id', 'pmid', 'dblp_key', 'openalex_id',
                         'ss_id', 'core_id', 'inspire_id', 'isbn')
    paper['authenticity_failed_identifier_type'] = (
        next((t for t in _id_type_priority if ids.get(t)), None) if identifier_failed else None)
    paper['verified_publisher'] = (
        (crossref or {}).get('publisher') or (openalex or {}).get('publisher')
        or (crossref_meta or {}).get('publisher') or (openalex_meta or {}).get('publisher')
        or (openalex_id_result or {}).get('publisher') or (core_id_result or {}).get('publisher') or None)
    paper['verified_issn'] = ((crossref or {}).get('issn') or (openalex or {}).get('issn')
                              or (s2 or {}).get('issn') or (crossref_meta or {}).get('issn')
                              or (openalex_meta or {}).get('issn')
                              or (openalex_id_result or {}).get('issn')
                              or (pmid_result or {}).get('issn') or [])
    # A bibliographic-search match found a DOI this paper's own source
    # never gave it — genuinely useful (lets downstream Crossref/OpenAlex
    # network checks run on a later pass), never invented.
    matched_doi = (crossref_meta or {}).get('matched_doi') or (openalex_meta or {}).get('matched_doi')
    if matched_doi and not paper.get('doi'):
        paper['doi_discovered_via_bibliographic_search'] = matched_doi
    paper['publisher_verified_by'] = next(
        (label for result, label in (
            (crossref, 'Crossref'), (openalex, 'OpenAlex'),
            (crossref_meta, 'Crossref (bibliographic search)'),
            (openalex_meta, 'OpenAlex (bibliographic search)'),
            (openalex_id_result, 'OpenAlex (ID lookup)'), (core_id_result, 'CORE'))
         if (result or {}).get('publisher')), None)
    return paper


def check_retraction(paper, crossref=None, openalex=None, s2=None,
                     pmid_result=None, openalex_id_result=None):
    """Sets `is_retracted` (True / False / None) and `retraction_source`.
    None means "not checked" — it is never collapsed into False."""
    signals = []
    if openalex and openalex.get('found'):
        signals.append(('OpenAlex is_retracted', bool(openalex.get('retracted'))))
    if crossref and crossref.get('found'):
        signals.append(('Crossref updated-by', bool(crossref.get('retracted'))))
    if openalex_id_result and openalex_id_result.get('found'):
        signals.append(('OpenAlex is_retracted (ID lookup)', bool(openalex_id_result.get('retracted'))))
    if pmid_result and pmid_result.get('found') and pmid_result.get('retracted') is not None:
        signals.append(('PubMed publication type (PMID lookup)', bool(pmid_result.get('retracted'))))
    # Some sources carry it natively in their own publication-type list
    # (PubMed's "Retracted Publication", Europe PMC's equivalent).
    native = [str(t).lower() for t in ((paper.get('publication_types_native') or [])
                                       + ((s2 or {}).get('publication_types') or [])
                                       + ((pmid_result or {}).get('publication_types') or []))]
    if native:
        signals.append(('source publication type',
                        any('retract' in t or 'withdraw' in t for t in native)))
    if not signals:
        paper['is_retracted'] = None
        paper['retraction_source'] = None
        return paper
    hits = [name for name, flagged in signals if flagged]
    paper['is_retracted'] = bool(hits)
    paper['retraction_source'] = hits or [name for name, _ in signals]
    return paper


# ─── Publication type ───────────────────────────────────────────────

def classify_publication_type(paper, crossref=None, openalex=None, s2=None,
                              pmid_result=None, openalex_id_result=None,
                              crossref_meta=None, openalex_meta=None):
    """
    Sets `publication_type`, `publication_type_source` and
    `peer_reviewed` (True / False / 'UNKNOWN').

    Registry metadata wins over heuristics; heuristics on the venue
    string are the last resort and are labelled as such, so a viva
    answer can distinguish "Crossref says proceedings-article" from
    "the venue string contained the word 'Workshop'".
    """
    ptype, origin = None, None

    cr_type = (crossref or {}).get('type')
    if cr_type and cr_type in _CROSSREF_TYPE_MAP:
        ptype, origin = _CROSSREF_TYPE_MAP[cr_type], f'Crossref type={cr_type}'

    if ptype is None:
        oa_type = (openalex or {}).get('type') or (openalex_id_result or {}).get('type')
        if oa_type and oa_type in _OPENALEX_TYPE_MAP:
            ptype, origin = _OPENALEX_TYPE_MAP[oa_type], f'OpenAlex type={oa_type}'

    if ptype is None:
        native = [str(t) for t in ((paper.get('publication_types_native') or [])
                                   + ((s2 or {}).get('publication_types') or [])
                                   + ((pmid_result or {}).get('publication_types') or []))]
        # Real lookup against NATIVE_TYPE_MAP (normalized exact-string
        # match), not a guessed substring search — so "Case Reports"
        # (PubMed) and "CaseReport" (Semantic Scholar) are recognized
        # as CASE_REPORT instead of silently falling through to OTHER,
        # and a record tagged with several native types at once picks
        # the most specific one via _NATIVE_TYPE_PRIORITY.
        matched = _classify_from_native_types(native)
        if matched is not None:
            ptype, origin = matched, f'publicationTypes={native}'

    venue_blob = f"{paper.get('venue') or ''} {paper.get('source') or ''}"
    if ptype is None:
        if _PREPRINT_VENUE_RE.search(venue_blob) or (paper.get('source') == 'ArXiv'):
            ptype, origin = PREPRINT, 'preprint server (venue/source string)'
        elif _CONFERENCE_VENUE_RE.search(venue_blob):
            ptype, origin = CONFERENCE_PAPER, 'venue string heuristic'
        elif _JOURNAL_VENUE_RE.search(venue_blob):
            ptype, origin = JOURNAL_ARTICLE, 'venue string heuristic'
        else:
            ptype, origin = OTHER, 'no registry type and no venue signal'

    # A registry type of "posted-content"/"preprint" that ALSO has a
    # journal/proceedings venue means the preprint was later published;
    # prefer the published form, and say why.
    if ptype == PREPRINT and _CONFERENCE_VENUE_RE.search(venue_blob) \
            and not _PREPRINT_VENUE_RE.search(venue_blob):
        ptype, origin = CONFERENCE_PAPER, origin + ' + published venue found'

    if ptype in (JOURNAL_ARTICLE, CONFERENCE_PAPER):
        match = _REVIEW_TITLE_RE.search(paper.get('title') or '')
        if match:
            # A "survey" and a "review" are both secondary literature but
            # your note lists them separately, so they stay separate here
            # and the title word decides which.
            word = (match.group(0) or '').lower()
            ptype = SURVEY if 'survey' in word or 'state' in word else REVIEW
            origin = (origin or '') + f' + secondary-literature title pattern ("{match.group(0).strip()}")'

    action, _weight = TYPE_POLICY.get(ptype, TYPE_POLICY[OTHER])
    paper['publication_type'] = ptype
    paper['publication_type_source'] = origin
    paper['publication_type_policy'] = action
    # Peer review status is asserted only where it is actually known.
    # SURVEY, CASE_REPORT and CLINICAL_TRIAL are as eligible as
    # JOURNAL_ARTICLE/CONFERENCE_PAPER/REVIEW here — all are published,
    # peer-reviewed research types once an independent registry
    # corroborates the record; only PREPRINT/THESIS are asserted False,
    # and every other type (DATASET/EDITORIAL/NEWS/OTHER/BOOK*) is
    # correctly left UNKNOWN rather than guessed at.
    corroborating_lookup_found = any((r or {}).get('found') for r in
        (crossref, openalex, s2, crossref_meta, openalex_meta,
         pmid_result, openalex_id_result))
    if ptype in (PREPRINT, THESIS):
        paper['peer_reviewed'] = False
    elif ptype in (JOURNAL_ARTICLE, CONFERENCE_PAPER, REVIEW, SURVEY,
                  CASE_REPORT, CLINICAL_TRIAL) and corroborating_lookup_found:
        paper['peer_reviewed'] = True
    else:
        paper['peer_reviewed'] = 'UNKNOWN'
    return paper


def compute_metadata_completeness(paper, crossref=None, openalex=None, s2=None):
    """Sets `metadata_completeness` (0-1) and `metadata_missing`."""
    abstract = (paper.get('abstract') or '').strip()
    present = {
        'title': bool((paper.get('title') or '').strip()),
        'authors': bool(paper.get('authors')),
        'year': _year_int(paper.get('year')) is not None,
        'venue': bool((paper.get('venue') or '').strip()),
        'identifier': bool(_paper_identifiers(paper)),
        'abstract': bool(abstract) and abstract.lower() != 'no abstract available.',
        'publisher': bool(paper.get('verified_publisher')),
        'publication_type': paper.get('publication_type') not in (None, OTHER),
    }
    paper['metadata_completeness'] = round(sum(present.values()) / len(present), 3)
    paper['metadata_present'] = present
    paper['metadata_missing'] = sorted(k for k, v in present.items() if not v)
    return paper


# ─── Level 2: relevance ─────────────────────────────────────────────

def _token_overlap(a_tokens, b_tokens):
    if not a_tokens or not b_tokens:
        return None
    a, b = set(a_tokens), set(b_tokens)
    return len(a & b) / len(a | b)


def compute_relevance_scores(papers, topic, keywords=None, research_question=None,
                             idea_description=None):
    """
    Level 2, for the whole set at once (TF-IDF needs the corpus).

    Sets `relevance_score` (0-100) and `relevance_components` on every
    paper. Components with no data on a given paper (no abstract, no
    native keywords) are dropped and the remaining weights renormalized
    — the same rule the impact score uses.

    `tfidf_cosine` is lexical TF-IDF cosine, not a transformer
    embedding. See the honesty note at the top of this file.
    """
    query_parts = [topic or '']
    if isinstance(keywords, (list, tuple)):
        query_parts.append(' '.join(str(k) for k in keywords))
    elif keywords:
        query_parts.append(str(keywords))
    if research_question:
        query_parts.append(str(research_question))
    if idea_description:
        query_parts.append(str(idea_description))
    query = ' '.join(p for p in query_parts if p).strip()
    query_tokens = tokenize(query)
    topic_tokens = tokenize(topic or '')

    docs = [query] + [
        ' '.join([p.get('title') or '',
                  p.get('abstract') or '',
                  ' '.join(str(k) for k in (p.get('keywords_native') or [])),
                  ' '.join(str(c) for c in (p.get('categories') or []))])
        for p in papers
    ]
    vectors = tfidf_vectors(docs)
    qv = vectors[0]
    tfidf_cosines = [cosine(qv, vec) for vec in vectors[1:]]

    # Semantic similarity: real sentence-transformer cosine when the
    # model is available in this deployment, TF-IDF cosine (already
    # computed above, at no extra cost) when it is not. One method for
    # the whole batch — see semantic_similarity.compute_semantic_similarities.
    semantic_texts = [(p.get('title') or '') + '. ' + (p.get('abstract') or '') for p in papers]
    semantic_values, semantic_method = semantic_similarity.compute_semantic_similarities(
        query, semantic_texts, tfidf_cosines)

    cross_values = semantic_similarity.cross_encoder_scores(query, semantic_texts)   # None when not configured
    if cross_values is not None and RELEVANCE_WEIGHTS.get('cross_encoder', 0.0) <= 0:
        RELEVANCE_WEIGHTS['cross_encoder'] = 0.20
    for idx, (paper, vec, tfidf_value, semantic_value) in enumerate(
            zip(papers, vectors[1:], tfidf_cosines, semantic_values)):
        raw = {}

        title_overlap = _token_overlap(query_tokens, tokenize(paper.get('title') or ''))
        if title_overlap is not None:
            raw['title_similarity'] = title_overlap

        abstract = (paper.get('abstract') or '').strip()
        if abstract and abstract.lower() != 'no abstract available.':
            overlap = _token_overlap(query_tokens, tokenize(abstract))
            if overlap is not None:
                # Jaccard against a long abstract is structurally small;
                # scale by the share of QUERY terms the abstract covers,
                # which is the thing actually being asked.
                covered = len(set(query_tokens) & set(tokenize(abstract))) / max(1, len(set(query_tokens)))
                raw['abstract_similarity'] = max(overlap, covered)

        native_kw = [str(k) for k in (paper.get('keywords_native') or []) if k]
        if native_kw:
            kw_tokens = tokenize(' '.join(native_kw))
            covered = len(set(query_tokens) & set(kw_tokens)) / max(1, len(set(query_tokens)))
            raw['keyword_overlap'] = covered

        categories = [str(c) for c in (paper.get('categories') or []) if c and c != 'Academic Paper']
        if categories and topic_tokens:
            cat_tokens = tokenize(' '.join(categories))
            raw['topic_overlap'] = len(set(topic_tokens) & set(cat_tokens)) / max(1, len(set(topic_tokens)))

        raw['semantic_similarity'] = semantic_value
        paper['relevance_similarity_method'] = semantic_method
        if cross_values is not None:
            # sigmoid in 0-1, halved because the blend below treats a raw 0.5 as "clearly on topic"
            raw['cross_encoder'] = cross_values[idx] * 0.5

        available = sum(RELEVANCE_WEIGHTS[c] for c in raw)
        score, components = 0.0, {}
        if available > 0:
            for component, value in raw.items():
                weight = RELEVANCE_WEIGHTS[component] / available
                # min(1, x/0.5): a 50% overlap with the query already
                # means "clearly on topic" — real abstracts never reach
                # a raw 1.0 against a short query.
                contribution = min(1.0, value / 0.5) * 100
                score += contribution * weight
                components[component] = {'value': round(value, 4),
                                         'weight_pct': round(weight * 100, 1)}
        paper['relevance_score'] = round(score, 2)
        paper['relevance_components'] = components
        paper['relevance_available_weight_pct'] = round(available * 100, 1)
    return papers


# ─── Level 3: quality ───────────────────────────────────────────────

def compute_quality_score(paper, crossref=None, openalex=None, s2=None, current_year=None):
    """
    Level 3. Sets `quality_score` (0-100) and `quality_components`.
    Every component is dropped rather than zero-filled when this paper
    has no real data for it, and remaining weights are renormalized.
    """
    current_year = current_year or _dt.datetime.now().year
    raw = {}

    # --- venue quality: real OpenAlex venue citedness (already enriched
    # in deep_collection), plus indexing/DOAJ/strict-venue evidence.
    venue_signals = []
    # Strongest evidence first: the VENUE's own registry record
    # (venue_registry.py — OpenAlex /sources h-index, 2-year mean
    # citedness, CWTS-Leiden core membership, DOAJ indexing, source
    # type). This is a real lookup of the venue itself rather than an
    # inference from whatever the paper record happened to carry, so
    # when it exists it replaces the weaker paper-level signals below
    # instead of competing with them. There is deliberately no
    # quartile here — see venue_registry.QUARTILE_BASIS.
    registry_venue = paper.get('venue_quality_registry')
    if registry_venue is not None:
        raw['venue_quality'] = registry_venue
        paper['venue_quality_basis'] = (
            f"OpenAlex /sources venue record ({paper.get('venue_lookup_basis')})")
    venue_influence = paper.get('venue_influence')
    if venue_influence is not None and venue_influence >= 0:
        venue_signals.append(min(1.0, math.log1p(venue_influence) / 3.0))
    if (openalex or {}).get('in_doaj'):
        venue_signals.append(0.85)
    if (openalex or {}).get('source_type') in ('journal', 'conference'):
        venue_signals.append(0.7)
    if (s2 or {}).get('venue_type') in ('journal', 'conference'):
        venue_signals.append(0.7)
    if paper.get('verified_issn'):
        venue_signals.append(0.6)
    if any(v in f"{paper.get('source') or ''} {paper.get('venue') or ''}" for v in _STRICT_VENUES):
        venue_signals.append(1.0)
    if set(paper.get('also_indexed_by') or []) & _INDEXED_SOURCES:
        venue_signals.append(0.65)
    if venue_signals and 'venue_quality' not in raw:
        raw['venue_quality'] = max(venue_signals)
        paper.setdefault('venue_quality_basis',
                         'paper-level venue signals only (venue not found in OpenAlex /sources)')

    # --- citation impact and recency, from the real citation count only
    citation_count = paper.get('citation_count')
    try:
        citation_count = float(citation_count) if citation_count is not None else None
    except (ValueError, TypeError):
        citation_count = None
    year = _year_int(paper.get('year'))
    age = max(1, current_year - year + 1) if year else None
    if citation_count is not None and citation_count >= 0:
        raw['citation_impact'] = min(1.0, math.log1p(citation_count) / math.log1p(500))
        # Semantic Scholar's real influentialCitationCount, where it
        # exists, is a better impact signal than raw volume — a citation
        # the citing paper actually built on, not one in a related-work
        # list. Averaged in rather than replacing the count outright.
        influential = (s2 or {}).get('influential_citation_count')
        if influential is not None:
            paper['influential_citation_count'] = influential
            raw['citation_impact'] = (raw['citation_impact']
                                      + min(1.0, math.log1p(influential) / math.log1p(50))) / 2
        if age:
            velocity = citation_count / age
            raw['citation_recency'] = min(1.0, math.log1p(velocity) / math.log1p(50))

    # --- methodology: only when full-text/abstract analysis actually ran
    analysis = paper.get('deep_analysis') or {}
    if isinstance(analysis, dict) and analysis:
        filled = sum(1 for k in ('methodology', 'datasets', 'limitations', 'future_work')
                     if (analysis.get(k) or '').strip() if isinstance(analysis.get(k), str))
        list_filled = sum(1 for k in ('methodology', 'datasets', 'limitations', 'future_work')
                          if isinstance(analysis.get(k), (list, tuple)) and analysis.get(k))
        raw['methodology'] = (filled + list_filled) / 4.0

    completeness = paper.get('metadata_completeness')
    if completeness is not None:
        raw['metadata_completeness'] = completeness

    ptype = paper.get('publication_type')
    if ptype:
        raw['publication_type'] = TYPE_POLICY.get(ptype, TYPE_POLICY[OTHER])[1]

    retracted = paper.get('is_retracted')
    if retracted is not None:
        raw['retraction_status'] = 0.0 if retracted else 1.0

    author_signals = []
    # Real ORCID resolution (author_verification.py) is the strongest
    # author signal available and the only one that can be NEGATIVE:
    # an iD that fails its check digit, or resolves to nobody, drags
    # this component down instead of merely failing to raise it. The
    # h-index/works-count signals below can only ever raise a score,
    # so they are combined with max() only among themselves and are
    # averaged with the ORCID finding rather than allowed to mask it.
    orcid_value = paper.get('author_verification_value')
    if paper.get('author_max_h_index') is not None:
        author_signals.append(min(1.0, math.log1p(paper['author_max_h_index']) / math.log1p(80)))
    # Real OpenAlex works_count — "does this author have other real
    # publications" (doc section 13), independent of h-index/citations.
    if paper.get('author_max_works_count') is not None:
        author_signals.append(min(1.0, math.log1p(paper['author_max_works_count']) / math.log1p(150)))
    if orcid_value is None and ((crossref or {}).get('orcids') or (openalex or {}).get('orcids')
                                or (s2 or {}).get('orcids')):
        # Fallback ONLY when ORCID resolution did not run for this paper
        # (verification disabled, or over the per-run ORCID budget).
        # When it did run, the presence of an iD tells us nothing that
        # the resolved result does not tell us better.
        author_signals.append(0.55)
        paper['author_verification_basis'] = ('ORCID iD present in the registry record but not '
                                              'resolved this run (budget/disabled) — presence only')
    if (crossref or {}).get('affiliations') or (openalex or {}).get('affiliations') \
            or paper.get('institution_ids_openalex') or paper.get('author_verified_affiliations'):
        author_signals.append(0.6)
    if orcid_value is not None and author_signals:
        raw['author_affiliation'] = round((orcid_value + max(author_signals)) / 2, 4)
    elif orcid_value is not None:
        raw['author_affiliation'] = orcid_value
    elif author_signals:
        raw['author_affiliation'] = max(author_signals)

    available = sum(QUALITY_WEIGHTS[c] for c in raw)
    score, components = 0.0, {}
    if available > 0:
        for component, value in raw.items():
            weight = QUALITY_WEIGHTS[component] / available
            score += value * 100 * weight
            components[component] = {'value': round(value, 4),
                                     'weight_pct': round(weight * 100, 1)}

    paper['quality_score'] = round(score, 2)
    paper['quality_components'] = components
    paper['quality_available_weight_pct'] = round(available * 100, 1)
    # Set by verify_papers BEFORE the ORCID pass so it has iDs to
    # resolve; only filled in here for callers that use this function
    # standalone, so the merged/deduplicated list is never clobbered.
    if not paper.get('author_orcids'):
        paper['author_orcids'] = ((crossref or {}).get('orcids')
                                  or (openalex or {}).get('orcids')
                                  or (s2 or {}).get('orcids') or [])
    if not paper.get('author_affiliations'):
        paper['author_affiliations'] = ((crossref or {}).get('affiliations')
                                        or (openalex or {}).get('affiliations') or [])
    return paper


# ─── Verdict ────────────────────────────────────────────────────────

def decide_verdict(paper):
    """
    The gate. Sets `verification_status` and `verification_reasons`.

    REJECTED only on POSITIVE evidence of a problem. Everything else
    that scores badly is kept and labelled, because "we could not check
    this" and "we checked this and it failed" are different claims and
    this pipeline must not conflate them. See honesty note 2 at the top
    of this file.
    """
    reasons = []
    status = None

    # The doc's SOURCE TRUST CHECK: a record that did not arrive through
    # an authorized source's official API never gets scored at all.
    if paper.get('source_authorized') is False:
        paper['verification_status'] = 'REJECTED'
        paper['verification_reasons'] = [paper.get('source_authorized_detail')
                                         or 'source is not in the authorized registry']
        paper['passed_verification'] = False
        paper['overall_score'] = 0.0
        return paper

    if paper.get('is_retracted') is True:
        status = 'RETRACTED'
        reasons.append(f"retraction recorded by {', '.join(paper.get('retraction_source') or ['registry'])}")

    # Two or more core fields disagreeing with an independent registry is
    # the fake/recycled-DOI signature. ONE disagreeing field is not: a
    # truncated title, a translated title, or an author list one registry
    # abbreviated will all trip a single check on a perfectly real paper.
    # So one contradiction downgrades and is reported; two reject.
    contradictions = paper.get('authenticity_contradictions') or []
    if status is None and len(contradictions) >= 2:
        status = 'REJECTED'
        reasons.append('metadata contradicted by an independent registry on '
                       + ', '.join(contradictions) + ' — the DOI resolves to a different work')

    severe = paper.get('authenticity_severe_mismatches') or []
    if (status is None and STRICT_FIELD_REJECT and severe
            and paper.get('authenticity_basis') in ('registry_verified', 'bibliographic_match_verified')):
        status = 'REJECTED'
        reasons.append('strict mode: ' + ' and '.join(severe) + ' on this record is unrelated to the '
                       'registry record its identifier resolves to')

    if status is None and paper.get('authenticity_identifier_unresolved'):
        id_type = paper.get('authenticity_failed_identifier_type')
        detail = paper.get('authenticity_checks', {}).get('identifier_resolves', {}).get('detail')
        status = 'REJECTED'
        label = {'doi': 'the DOI', 'arxiv_id': 'the arXiv ID', 'dblp_key': 'the DBLP key'}.get(
            id_type, 'the identifier')
        reasons.append(f"{label} on this record does not resolve"
                       + (f" ({detail})" if detail else ""))

    if status is None and paper.get('publication_type_policy') == 'exclude':
        status = 'REJECTED'
        reasons.append(f"publication type {paper.get('publication_type')} is not a research paper")

    relevance = paper.get('relevance_score')
    if status is None and relevance is not None and relevance < RELEVANCE_DISCARD_THRESHOLD:
        status = 'OFF_TOPIC'
        reasons.append(f"relevance {relevance} below threshold {RELEVANCE_DISCARD_THRESHOLD}")

    authenticity = paper.get('authenticity_score') or 0
    if status is None:
        if authenticity >= 90:
            status, note = 'HIGHLY_VERIFIED', 'authenticity >= 90'
        elif authenticity >= AUTHENTICITY_REJECT_THRESHOLD:
            status, note = 'VERIFIED', f'authenticity >= {AUTHENTICITY_REJECT_THRESHOLD}'
        elif authenticity >= 60:
            status, note = 'PARTIALLY_VERIFIED', 'authenticity 60-74'
        else:
            status, note = 'UNVERIFIED', 'authenticity < 60'
        reasons.append(note)
        if paper.get('authenticity_basis') == 'uncorroborated_single_source':
            reasons.append('no independent registry corroborated this record — score is capped, '
                           'not a finding of fraud')
        elif paper.get('authenticity_basis') == 'offline_only':
            reasons.append('outside the per-paper network-verification budget — scored on '
                           'cross-source agreement only')
        if len(contradictions) == 1:
            status = 'PARTIALLY_VERIFIED' if status in ('HIGHLY_VERIFIED', 'VERIFIED') else status
            reasons.append(f"one field ({contradictions[0]}) disagrees with an independent "
                           f"registry — not enough to reject, but the record is not fully "
                           f"corroborated")
        if paper.get('publication_type_policy') == 'flag':
            reasons.append(f"{paper.get('publication_type')} — peer review status: "
                           f"{paper.get('peer_reviewed')}")

    paper['verification_status'] = status
    paper['verification_reasons'] = reasons
    paper['passed_verification'] = status not in ('REJECTED', 'RETRACTED', 'OFF_TOPIC')

    weights = OVERALL_WEIGHTS
    blended = round(
        weights['authenticity'] * (paper.get('authenticity_score') or 0)
        + weights['relevance'] * (paper.get('relevance_score') or 0)
        + weights['quality'] * (paper.get('quality_score') or 0), 2)
    # A paper that failed the gate must not carry a high overall score.
    # The recycled-DOI case is the reason: its title/abstract match the
    # query perfectly and its (stolen) venue looks excellent, so it
    # blends to ~95 — which would put a rejected record at the top of
    # any ranking or per-source average that sorts on overall_score.
    # The four component scores are kept intact for the audit trail;
    # only the combined figure is zeroed, and `overall_score_blended`
    # preserves what it would have been.
    paper['overall_score_blended'] = blended
    paper['overall_score'] = blended if paper['passed_verification'] else 0.0
    return paper


ACCEPT_STATUSES = frozenset({'HIGHLY_VERIFIED', 'VERIFIED'})
REJECT_STATUSES = frozenset({'REJECTED', 'RETRACTED', 'OFF_TOPIC'})


def decision_band(paper):
    """Collapse the seven verdicts into the three-way decision used for evaluation and the UI:
    'accept' (HIGHLY_VERIFIED / VERIFIED), 'reject' (REJECTED / RETRACTED / OFF_TOPIC) and
    'review' (PARTIALLY_VERIFIED / UNVERIFIED / anything else) - the system abstains and a human looks.
    A confident accept/reject is the "decisive" verdict whose accuracy eval/ reports."""
    status = paper.get('verification_status')
    if status in ACCEPT_STATUSES:
        return 'accept'
    if status in REJECT_STATUSES:
        return 'reject'
    return 'review'


# ─── Top-level entry point ──────────────────────────────────────────

_UNKNOWN_ON_ERROR = {'isbn_resolves': None, 'dblp_resolves': None,
                     'openalex_lookup_batch': {}, 'semantic_scholar_lookup_batch': {}}


def _guarded(name):
    """Look a registry function up at call time and make it incapable of aborting the run.

    An unexpected exception from a registry client (a bug, a malformed response, a timeout that escaped
    the client's own handling) becomes the same "could not check" answer an unreachable registry gives:
    {'found': False, 'error': ...} for single lookups, {} for batches, None for the True/False/None
    resolvers. "Could not check" never counts as a failed check - see honesty note 2 at the top."""
    def call(*args, **kwargs):
        try:
            return globals()[name](*args, **kwargs)
        except Exception as e:
            print(f"[verify] {name} raised {type(e).__name__}: {e} - treated as 'could not check'")
            if name in _UNKNOWN_ON_ERROR:
                return _UNKNOWN_ON_ERROR[name]
            return {'found': False, 'error': f'{type(e).__name__}: {e}'}
    return call


def verify_papers(papers, topic, keywords=None, research_question=None,
                  idea_description=None, network_top_n=None, progress_cb=None):
    """
    Runs the whole three-level pipeline over `papers` (the Stage-2
    output of deep_collection). Mutates each paper in place and returns
    a summary dict.

    Papers are NOT removed from the list — every paper keeps its four
    scores and its `verification_status`, and the caller decides what to
    do with the rejected ones. Callers that want the filtered set can
    use the returned `kept` / `rejected` lists.

    Network cost: one batched OpenAlex request per 50 DOIs across the
    whole set, plus one Crossref request per DOI for the top
    `network_top_n` papers by impact rank (default
    VERIFY_NETWORK_TOP_N).
    """
    papers = list(papers or [])
    if not papers:
        return {'verified': 0, 'kept': [], 'rejected': [], 'status_counts': {}}

    network_top_n = VERIFY_NETWORK_TOP_N if network_top_n is None else network_top_n
    ranked = sorted(papers, key=lambda p: p.get('impact_score') or 0, reverse=True)
    network_slice = ranked[:network_top_n] if network_top_n else ranked
    network_ids = {id(p) for p in network_slice}

    # --- Level 2 first: it is free, needs the whole corpus for IDF, and
    # tells us nothing about authenticity either way.
    if progress_cb:
        progress_cb('Scoring topical relevance (TF-IDF, no network calls)...')
    compute_relevance_scores(papers, topic, keywords=keywords,
                             research_question=research_question,
                             idea_description=idea_description)

    # --- OpenAlex: batched, covers every paper that has a DOI.
    if progress_cb:
        progress_cb('Cross-checking DOIs against OpenAlex (batched)...')
    all_dois = [p.get('doi') for p in papers if _norm_doi(p.get('doi'))]
    openalex_by_doi = _guarded('openalex_lookup_batch')(all_dois) if all_dois else {}

    # --- Semantic Scholar: batched third registry, keyed on DOI OR
    # arXiv ID, so it also corroborates papers Crossref can never see.
    if progress_cb:
        progress_cb('Cross-checking identifiers against Semantic Scholar (batched)...')
    s2_by_key = _guarded('semantic_scholar_lookup_batch')(papers)
    # Which S2 lookups genuinely got an answer (see the note in
    # semantic_scholar_lookup_batch). A key that was never answered
    # must score as "not checked", not as "does not exist".
    s2_answered = s2_by_key.pop('__answered__', set())

    # --- Crossref: one request per DOI, bounded to the top-N by impact.
    if progress_cb:
        progress_cb(f'Verifying DOIs against Crossref (top {len(network_slice)} by impact)...')
    crossref_targets = [p for p in network_slice if _norm_doi(p.get('doi'))]
    crossref_by_doi = {}
    if crossref_targets:
        with ThreadPoolExecutor(max_workers=max(1, VERIFY_MAX_WORKERS)) as pool:
            for doi, result in zip(
                    [_norm_doi(p.get('doi')) for p in crossref_targets],
                    pool.map(lambda p: _guarded('crossref_lookup')(p.get('doi')), crossref_targets)):
                crossref_by_doi[doi] = result

    # --- DBLP resolution: same network budget as Crossref, and only
    # for papers with no DOI to check instead (a paper with a DOI is
    # already verified through the stronger Crossref/OpenAlex path;
    # this is specifically the strict-venue-paper-with-no-DOI case).
    dblp_targets = [p for p in network_slice
                    if not _norm_doi(p.get('doi')) and (p.get('dblp_key') or '').strip()]
    dblp_resolved_by_key = {}
    if dblp_targets:
        if progress_cb:
            progress_cb(f'Verifying DBLP keys ({len(dblp_targets)})...')
        with ThreadPoolExecutor(max_workers=max(1, VERIFY_MAX_WORKERS)) as pool:
            for key, result in zip(
                    [p.get('dblp_key') for p in dblp_targets],
                    pool.map(lambda p: _guarded('dblp_resolves')(p.get('dblp_key')), dblp_targets)):
                dblp_resolved_by_key[key] = result

    # --- PMID resolution: NCBI ESummary, bounded to the network budget,
    # for no-DOI papers carrying a PMID (PubMed records collected via a
    # source other than the direct PubMed fetch, or any DOI-less
    # PubMed record — the DOI/OpenAlex/Crossref path already covers
    # the common case of a PubMed paper that also has a DOI).
    pmid_targets = [p for p in network_slice
                    if not _norm_doi(p.get('doi')) and (p.get('pmid') or '').strip()]
    pmid_by_id = {}
    if pmid_targets:
        if progress_cb:
            progress_cb(f'Verifying PMIDs against NCBI ({len(pmid_targets)})...')
        with ThreadPoolExecutor(max_workers=max(1, VERIFY_MAX_WORKERS)) as pool:
            for pmid, result in zip([p.get('pmid') for p in pmid_targets],
                                    pool.map(lambda p: _guarded('pubmed_lookup')(p.get('pmid')), pmid_targets)):
                pmid_by_id[pmid] = result

    # --- OpenAlex ID / S2 paper ID / CORE ID / INSPIRE ID resolution:
    # each is a real single-record lookup against that registry's own
    # store, tried only for no-DOI papers that still have no resolved
    # identifier from a higher-priority check above (arXiv/PMID/DBLP),
    # so this doesn't spend network budget on papers already resolved.
    def _still_unresolved(p):
        if _norm_doi(p.get('doi')):
            return False
        if (p.get('arxiv_id') or '').strip() and _ss_key(p) in s2_by_key:
            return False
        if (p.get('pmid') or '').strip() and pmid_by_id.get(p.get('pmid')):
            return False
        if (p.get('dblp_key') or '').strip() and dblp_resolved_by_key.get(p.get('dblp_key')) is not None:
            return False
        return True

    remaining = [p for p in network_slice if _still_unresolved(p)]
    oaid_targets = [p for p in remaining if (p.get('openalex_id') or '').strip()]
    ssid_targets = [p for p in remaining if (p.get('ss_id') or '').strip()]
    core_targets = [p for p in remaining if (p.get('core_id') or '').strip()]
    inspire_targets = [p for p in remaining if (p.get('inspire_id') or '').strip()]
    isbn_targets = [p for p in remaining if (p.get('isbn') or '').strip()]

    oaid_by_id, ssid_by_id, core_by_id, inspire_by_id, isbn_by_id = {}, {}, {}, {}, {}
    if oaid_targets or ssid_targets or core_targets or inspire_targets or isbn_targets:
        if progress_cb:
            progress_cb('Resolving remaining OpenAlex/S2/CORE/INSPIRE/ISBN identifiers...')
        with ThreadPoolExecutor(max_workers=max(1, VERIFY_MAX_WORKERS)) as pool:
            if oaid_targets:
                for p, r in zip(oaid_targets, pool.map(lambda p: _guarded('openalex_id_lookup')(p.get('openalex_id')), oaid_targets)):
                    oaid_by_id[p.get('openalex_id')] = r
            if ssid_targets:
                for p, r in zip(ssid_targets, pool.map(lambda p: _guarded('semantic_scholar_id_lookup')(p.get('ss_id')), ssid_targets)):
                    ssid_by_id[p.get('ss_id')] = r
            if core_targets:
                for p, r in zip(core_targets, pool.map(lambda p: _guarded('core_id_lookup')(p.get('core_id')), core_targets)):
                    core_by_id[p.get('core_id')] = r
            if inspire_targets:
                for p, r in zip(inspire_targets, pool.map(lambda p: _guarded('inspire_id_lookup')(p.get('inspire_id')), inspire_targets)):
                    inspire_by_id[p.get('inspire_id')] = r
            if isbn_targets:
                for p, r in zip(isbn_targets, pool.map(lambda p: _guarded('isbn_resolves')(p.get('isbn')), isbn_targets)):
                    isbn_by_id[p.get('isbn')] = r

    # --- Bibliographic search: genuine cross-source corroboration for
    # papers with NO DOI at all (DBLP, ERIC, Internet Archive, and any
    # strict-venue paper whose DBLP key merely resolved without a
    # second registry agreeing). Bounded to the same network budget —
    # this is the check that lets a DOI-less paper reach VERIFIED
    # instead of being permanently capped as uncorroborated.
    biblio_targets = [p for p in network_slice if not _norm_doi(p.get('doi'))]
    crossref_meta_by_id, openalex_meta_by_id = {}, {}
    if biblio_targets:
        if progress_cb:
            progress_cb(f'Searching Crossref/OpenAlex by title+author for {len(biblio_targets)} '
                       f'DOI-less paper(s)...')
        with ThreadPoolExecutor(max_workers=max(1, VERIFY_MAX_WORKERS)) as pool:
            cr_results = list(pool.map(crossref_bibliographic_search, biblio_targets))
            oa_results = list(pool.map(openalex_bibliographic_search, biblio_targets))
        for paper, cr_res, oa_res in zip(biblio_targets, cr_results, oa_results):
            crossref_meta_by_id[id(paper)] = cr_res
            openalex_meta_by_id[id(paper)] = oa_res

    if progress_cb:
        progress_cb('Scoring authenticity, publication type and retraction status...')
    current_year = _dt.datetime.now().year
    # Registry results are kept per paper so the LEVEL-3 pass below can
    # reuse them without a second round of lookups.
    registry_for = {}
    for paper in papers:
        doi = _norm_doi(paper.get('doi'))
        crossref = crossref_by_doi.get(doi)
        openalex = openalex_by_doi.get(doi)
        s2 = s2_by_key.get(_ss_key(paper))
        s2_answered_for_paper = _ss_key(paper) in s2_answered
        if s2 and s2.get('paper_id') and not paper.get('ss_id'):
            paper['ss_id'] = s2['paper_id']
        dblp_resolved = dblp_resolved_by_key.get(paper.get('dblp_key'))
        crossref_meta = crossref_meta_by_id.get(id(paper))
        openalex_meta = openalex_meta_by_id.get(id(paper))
        pmid_result = pmid_by_id.get(paper.get('pmid'))
        openalex_id_result = oaid_by_id.get(paper.get('openalex_id'))
        ss_id_result = ssid_by_id.get(paper.get('ss_id'))
        core_id_result = core_by_id.get(paper.get('core_id'))
        inspire_id_result = inspire_by_id.get(paper.get('inspire_id'))
        isbn_resolved = isbn_by_id.get(paper.get('isbn'))
        network_ran = id(paper) in network_ids

        check_source_authorized(paper)
        compute_authenticity(paper, crossref=crossref, openalex=openalex, s2=s2,
                             dblp_resolved=dblp_resolved, crossref_meta=crossref_meta,
                             openalex_meta=openalex_meta, pmid_result=pmid_result,
                             openalex_id_result=openalex_id_result, ss_id_result=ss_id_result,
                             core_id_result=core_id_result, inspire_id_result=inspire_id_result,
                             isbn_resolved=isbn_resolved, network_ran=network_ran,
                             s2_answered=s2_answered_for_paper)
        check_retraction(paper, crossref=crossref, openalex=openalex, s2=s2,
                        pmid_result=pmid_result, openalex_id_result=openalex_id_result)
        classify_publication_type(paper, crossref=crossref, openalex=openalex, s2=s2,
                                  pmid_result=pmid_result, openalex_id_result=openalex_id_result,
                                  crossref_meta=crossref_meta, openalex_meta=openalex_meta)
        compute_metadata_completeness(paper, crossref=crossref, openalex=openalex, s2=s2)
        # ORCIDs are collected here (not in compute_quality_score) so
        # the ORCID resolution pass below has something to resolve.
        paper['author_orcids'] = list(dict.fromkeys(
            ((crossref or {}).get('orcids') or [])
            + ((openalex or {}).get('orcids') or [])
            + ((s2 or {}).get('orcids') or [])
            + ((crossref_meta or {}).get('orcids') or [])
            + ((openalex_meta or {}).get('orcids') or [])
            + ((openalex_id_result or {}).get('orcids') or [])
            + ((ss_id_result or {}).get('orcids') or [])))
        paper['author_affiliations'] = ((crossref or {}).get('affiliations')
                                        or (openalex or {}).get('affiliations') or [])
        registry_for[id(paper)] = (crossref, openalex, s2)

    # ─── Venue-level and author-level verification ──────────────────
    # Both need Level 1 to have finished: the venue lookup keys on
    # `verified_issn` (set by compute_authenticity from the Crossref/
    # OpenAlex record, not from the search result), and the ORCID pass
    # needs the collected iDs above. Both are bounded, both annotate
    # papers in place, and both are wrapped so that a registry outage
    # downgrades to "not checked" instead of failing the whole run.
    try:
        venue_summary = venue_registry.enrich_venue_quality(papers, progress_cb=progress_cb)
    except Exception as e:
        print(f'paper_verification: venue registry pass failed, continuing without it: {e}')
        venue_summary = {'ran': False, 'error': str(e)}
    try:
        author_summary = author_verification.verify_authors(papers, progress_cb=progress_cb)
    except Exception as e:
        print(f'paper_verification: ORCID pass failed, continuing without it: {e}')
        author_summary = {'ran': False, 'error': str(e)}

    # ─── Level 3 + verdict ──────────────────────────────────────────
    if progress_cb:
        progress_cb('Scoring quality and deciding verdicts...')
    for paper in papers:
        crossref, openalex, s2 = registry_for.get(id(paper), (None, None, None))
        compute_quality_score(paper, crossref=crossref, openalex=openalex, s2=s2,
                              current_year=current_year)
        decide_verdict(paper)

    kept = [p for p in papers if p.get('passed_verification')]
    rejected = [p for p in papers if not p.get('passed_verification')]
    kept.sort(key=lambda p: p.get('overall_score') or 0, reverse=True)
    for rank, paper in enumerate(kept, start=1):
        paper['verified_rank'] = rank

    status_counts = {}
    for paper in papers:
        status_counts[paper['verification_status']] = status_counts.get(paper['verification_status'], 0) + 1
    type_counts = {}
    for paper in papers:
        type_counts[paper['publication_type']] = type_counts.get(paper['publication_type'], 0) + 1

    return {
        'verified': len(papers),
        'kept': kept,
        'rejected': rejected,
        'kept_count': len(kept),
        'rejected_count': len(rejected),
        'status_counts': status_counts,
        'publication_type_counts': type_counts,
        'retracted_count': sum(1 for p in papers if p.get('is_retracted') is True),
        'crossref_checked': len(crossref_by_doi),
        'crossref_resolved': sum(1 for r in crossref_by_doi.values() if r and r.get('found')),
        'crossref_unresolved': sum(1 for r in crossref_by_doi.values() if r and r.get('found') is False),
        'crossref_errors': sum(1 for r in crossref_by_doi.values() if r and r.get('error')),
        'openalex_matched': len(openalex_by_doi),
        'semantic_scholar_matched': len(s2_by_key),
        'dblp_checked': len(dblp_resolved_by_key),
        'dblp_resolved': sum(1 for v in dblp_resolved_by_key.values() if v is True),
        'dblp_unresolved': sum(1 for v in dblp_resolved_by_key.values() if v is False),
        'bibliographic_search_attempted': len(biblio_targets),
        'bibliographic_search_matched': sum(
            1 for r in list(crossref_meta_by_id.values()) + list(openalex_meta_by_id.values())
            if r and r.get('found')),
        'pmid_checked': len(pmid_by_id),
        'pmid_resolved': sum(1 for r in pmid_by_id.values() if r and r.get('found')),
        'other_identifier_checked': len(oaid_by_id) + len(ssid_by_id) + len(core_by_id) + len(inspire_by_id),
        'other_identifier_resolved': sum(
            1 for r in list(oaid_by_id.values()) + list(ssid_by_id.values())
            + list(core_by_id.values()) + list(inspire_by_id.values()) if r and r.get('found')),
        'unauthorized_source_count': sum(1 for p in papers if p.get('source_authorized') is False),
        'papers_without_doi': sum(1 for p in papers if not _norm_doi(p.get('doi'))),
        'network_verified_count': len(network_slice),
        'venue_verification': venue_summary,
        'semantic_similarity': semantic_similarity.model_status(),
        'cross_encoder': semantic_similarity.cross_encoder_status(),
        'author_verification': author_summary,
        'thresholds': {
            'authenticity_min': AUTHENTICITY_REJECT_THRESHOLD,
            'relevance_min': RELEVANCE_DISCARD_THRESHOLD,
            'uncorroborated_ceiling': UNCORROBORATED_CEILING,
        },
    }