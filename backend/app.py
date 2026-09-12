from flask import Flask, request, jsonify, send_file, send_from_directory, g
from flask_cors import CORS
import os, json, re, requests, time, sqlite3, xml.etree.ElementTree as ET

# Loads backend/.env (see .env.example) into os.environ before anything
# below reads GROQ_MODEL/TAVILY_API_KEY/etc. via os.environ.get(...).
# python-dotenv never overrides a variable that's already set in the
# real environment (e.g. a hosting platform's own env-var config), so
# this is safe to leave in for both local dev and production.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed — env vars must be set another way
import PyPDF2, networkx as nx, uuid, datetime
import smtplib
from email.mime.text import MIMEText
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from groq import Groq

# Groq deprecated llama-3.3-70b-versatile on 2026-08-16 (see
# https://console.groq.com/docs/deprecations); openai/gpt-oss-120b is
# their recommended production-tier replacement. Configurable via env
# var so the next deprecation is a one-line fix, not a find-and-replace.
GROQ_MODEL = os.environ.get('GROQ_MODEL', 'openai/gpt-oss-120b')
from werkzeug.utils import secure_filename
from auth import require_auth
from crypto_utils import encrypt_secret, decrypt_secret
from agents import (
    Orchestrator, SearchAgent, PDFAnalysisAgent, MultiPDFAnalysisAgent,
    KeywordExtractionAgent, KnowledgeGraphAgent, GapDetectionAgent,
    TrendAnalysisAgent, CitationAgent, ReportGenerationAgent, MonitoringAgent,
    RAGResearchAssistantAgent, NoveltyVerificationAgent, ResearchProposalAgent,
    ExperimentRecommendationAgent, ResearchImpactAgent, ExplainabilityAgent,
    MetadataAgent, EmbeddingAgent, ValidationAgent, PlannerAgent, MemoryAgent,
    DomainClassificationAgent, DeepCollectionAgent, TrendGapDetectionAgent,
    DomainUnderstandingAgent, CurrentTrendsAgent, FullTextAnalysisAgent,
    TrendForecastingAgent,
    trace_to_dicts,
)
from events import EventBus
from events.subscribers import register_default_subscribers
from memory import ContextMemory, VectorStore, TTLCache
from memory.context_memory import init_memory_table
from memory.vector_store import init_embeddings_table
from publications import (
    init_publication_tables, seed_publication_data, classify_query,
    register_publication_routes,
)
from academic_sources import FREE_SOURCES, KEYED_SOURCES, keyed_source_status, DBLP_VENUE_STREAMS, fetch_dblp_venue, \
    SPECIALIZED_SOURCE_DOMAINS, relevant_specialized_sources
import deep_collection
import current_trends
import full_text_analysis
from agents.textsim import compute_grounded_novelty
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY

app = Flask(__name__, static_folder='../frontend', static_url_path='')
CORS(app)

# On Render free tier, only /tmp is writable (ephemeral between deploys — acceptable for uploads/reports)
_base = os.environ.get('STORAGE_BASE', os.path.join(os.path.dirname(__file__), '..'))
UPLOAD_FOLDER  = os.path.join(_base, 'uploads')
REPORTS_FOLDER = os.path.join(_base, 'reports')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(REPORTS_FOLDER, exist_ok=True)

# On Render, use /tmp (writable); locally, use the project root next to backend/
import tempfile
_default_db = os.path.join(os.path.dirname(__file__), '..', 'researchlens.db')
DB_PATH = os.environ.get('DB_PATH', _default_db)

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def record_generated_file(filename, owner_uid):
    """Every file written into REPORTS_FOLDER must be recorded here so
    /api/download can enforce ownership instead of serving any filename
    to any caller."""
    conn = get_db()
    conn.execute(
        'INSERT OR REPLACE INTO generated_files (filename, owner_uid, created_at) VALUES (?, ?, ?)',
        (filename, owner_uid, datetime.datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

def init_db():
    conn = get_db()
    conn.execute('''CREATE TABLE IF NOT EXISTS analyses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        topic TEXT NOT NULL,
        user_id TEXT,
        analysis_json TEXT NOT NULL,
        papers_json TEXT,
        gaps_count INTEGER,
        created_at TEXT NOT NULL
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS watched_topics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        topic TEXT NOT NULL UNIQUE,
        user_id TEXT,
        api_key TEXT,
        notify_email TEXT,
        last_checked_at TEXT,
        last_gap_count INTEGER DEFAULT 0,
        last_paper_count INTEGER DEFAULT 0,
        created_at TEXT NOT NULL,
        active INTEGER DEFAULT 1
    )''')
    try:
        conn.execute('ALTER TABLE watched_topics ADD COLUMN notify_email TEXT')
    except Exception:
        pass  # column already exists
    conn.execute('''CREATE TABLE IF NOT EXISTS topic_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        watched_topic_id INTEGER NOT NULL,
        analysis_json TEXT NOT NULL,
        new_gaps_json TEXT,
        new_papers_count INTEGER DEFAULT 0,
        checked_at TEXT NOT NULL,
        FOREIGN KEY(watched_topic_id) REFERENCES watched_topics(id)
    )''')

    # ── Collaboration: lightweight join-code workspaces ──
    # NOTE: this is intentionally NOT real multi-tenant access control.
    # Anyone with the join_code has full read/write access to the
    # workspace — there is no per-user permission system because the app
    # has no real backend authentication yet (see earlier security review).
    # Fine for a trusted lab group; not safe for confidential unpublished
    # research against an adversary.
    conn.execute('''CREATE TABLE IF NOT EXISTS workspaces (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        join_code TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS workspace_members (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        workspace_id INTEGER NOT NULL,
        display_name TEXT NOT NULL,
        joined_at TEXT NOT NULL,
        FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS workspace_papers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        workspace_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        normalized_title TEXT NOT NULL,
        abstract TEXT,
        year TEXT,
        authors_json TEXT,
        source TEXT,
        topic TEXT,
        contributed_by TEXT,
        added_at TEXT NOT NULL,
        FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS gap_comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        workspace_id INTEGER NOT NULL,
        gap_title TEXT NOT NULL,
        author_name TEXT NOT NULL,
        comment_text TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS gap_assignments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        workspace_id INTEGER NOT NULL,
        gap_title TEXT NOT NULL,
        assigned_to TEXT NOT NULL,
        assigned_by TEXT,
        assigned_at TEXT NOT NULL,
        FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
    )''')

    # ── Public community layer: opt-in, read-only by design ──
    conn.execute('''CREATE TABLE IF NOT EXISTS public_gaps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        gap_title TEXT NOT NULL,
        description TEXT,
        topic TEXT,
        severity TEXT,
        contributor_name TEXT,
        owner_uid TEXT,
        shared_at TEXT NOT NULL,
        upvotes INTEGER DEFAULT 0
    )''')

    # ── Evaluation infrastructure ──
    # This is the actual first step toward "you cannot defend accuracy
    # claims without a gold-standard test set" — a small, real accuracy
    # log, not a fabricated benchmark. It only becomes meaningful once real
    # researchers use it and rate real gaps; it does nothing on its own.
    conn.execute('''CREATE TABLE IF NOT EXISTS gap_feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        topic TEXT,
        gap_title TEXT NOT NULL,
        severity TEXT,
        confidence_score INTEGER,
        evidence_verified INTEGER DEFAULT 0,
        verdict TEXT NOT NULL,
        rater_name TEXT,
        notes TEXT,
        rated_at TEXT NOT NULL
    )''')

    # ── Ownership record for generated files (reports / bibtex exports) ──
    # /api/download used to serve any filename in REPORTS_FOLDER to anyone
    # with no auth and no ownership check (path traversal + IDOR). Every
    # file we generate is now recorded here with the owner's Firebase uid,
    # and the download route checks both the path and this table.
    conn.execute('''CREATE TABLE IF NOT EXISTS generated_files (
        filename TEXT PRIMARY KEY,
        owner_uid TEXT NOT NULL,
        created_at TEXT NOT NULL
    )''')

    # Backfill user_id columns for older rows created before auth existed
    for table in ('analyses', 'watched_topics', 'public_gaps'):
        try:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN owner_uid TEXT')
        except Exception:
            pass  # column already exists

    # document_text: the extracted PDF plain text (truncated), stored so the
    # RAG Research Assistant Agent can answer follow-up questions about a
    # past analysis without requiring the PDF to be re-uploaded.
    try:
        conn.execute('ALTER TABLE analyses ADD COLUMN document_text TEXT')
    except Exception:
        pass  # column already exists

    # New tables for the Memory Agent (conversation/research/gap/proposal
    # memory) and the Embedding Agent's vector store (see memory/ package).
    init_memory_table(conn)
    init_embeddings_table(conn)

    # Publication Intelligence layer (domains/subdomains/societies/
    # publishers/journals/conferences). Own tables, additive, idempotent —
    # see publications.py / publication_data.py for the no-fake-data policy
    # governing journal/conference metric fields.
    init_publication_tables(conn)
    seed_publication_data(conn)

    conn.commit()
    conn.close()

init_db()

# ─── Email alerts for watched topics ─────────────────────────────────
SMTP_HOST = os.environ.get('SMTP_HOST', '')
SMTP_PORT = int(os.environ.get('SMTP_PORT', '587'))
SMTP_USER = os.environ.get('SMTP_USER', '')
SMTP_PASS = os.environ.get('SMTP_PASS', '')
ALERT_FROM_EMAIL = os.environ.get('ALERT_FROM_EMAIL', SMTP_USER)

def send_alert_email(to_email, topic, new_gaps, new_paper_count):
    """
    Notifies a user when their watched topic has genuinely new gaps or
    papers since the last check. Configured via env vars — if unset, this
    silently no-ops rather than crashing the scheduler.
    """
    if not to_email or not SMTP_HOST or not SMTP_USER or not SMTP_PASS:
        print(f"[Email] Skipped (SMTP not configured or no recipient) for topic '{topic}'")
        return False
    try:
        gap_lines = "\n".join(f"- {g.get('gap_title','')}" for g in new_gaps[:5])
        body = (
            f"Your watched topic \"{topic}\" has updates:\n\n"
            f"{len(new_gaps)} new research gap(s) detected:\n{gap_lines or '(none)'}\n\n"
            f"{new_paper_count} new related paper(s) found since your last check.\n\n"
            f"Log in to ResearchLens AI to see the full details.\n"
        )
        msg = MIMEText(body)
        msg['Subject'] = f'ResearchLens AI: updates on "{topic}"'
        msg['From'] = ALERT_FROM_EMAIL
        msg['To'] = to_email
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        print(f"[Email] Alert sent to {to_email} for topic '{topic}'")
        return True
    except Exception as e:
        print(f"[Email] Failed to send alert for '{topic}': {e}")
        return False

@app.route('/')
def root():
    return send_from_directory('../frontend', 'login.html')

@app.route('/index.html')
def index():
    return send_from_directory('../frontend', 'index.html')

@app.route('/login')
def login():
    return send_from_directory('../frontend', 'login.html')

@app.route('/explorer')
def explorer_page():
    return send_from_directory('../frontend', 'explorer.html')

# ─── ArXiv fetch ───────────────────────────────────────────────────
def _fetch_semantic_scholar(topic, max_results=50, venue=None, offset=0):
    """
    Real backup source: Semantic Scholar API (200M+ peer-reviewed papers).
    Used only when ArXiv is unreachable — returns real papers, never fake.
    API docs: https://api.semanticscholar.org/graph/v1

    `venue`: optional list of venue/conference names (e.g. ['NeurIPS','ICML']).
    Semantic Scholar's search endpoint officially supports a comma-separated
    `venue` filter param that restricts results to those exact publication
    venues — this is a real, documented filter, not a keyword hint.

    `offset`: the /paper/search endpoint documents `offset` + `limit` for
    pagination (paging is capped around the first ~1000 matches by Semantic
    Scholar itself, deep_collection.py's per-source cap respects that).
    Also requests `citationCount` — Semantic Scholar's own maintained
    citation count — surfaced as `citation_count` for impact scoring.

    Semantic Scholar's UNKEYED public tier is aggressively rate-limited
    (~100 requests/5 min shared across everyone hitting it without an
    api_key) so a plain 429 here is common and does NOT mean "no papers
    exist" — it means the request never actually ran. We retry a couple
    of times with backoff (honoring Retry-After when the server sends
    one), and if it still fails, we RAISE instead of swallowing the
    error, so the caller sees the real reason instead of a silent [].
    """
    query = topic.replace(' ', '+')
    url = (
        f"https://api.semanticscholar.org/graph/v1/paper/search"
        f"?query={query}&limit={min(max_results, 100)}&offset={max(0, offset)}"
        f"&fields=title,abstract,year,authors,externalIds,publicationTypes,venue,citationCount,fieldsOfStudy,references.title"
    )
    if venue:
        venue_param = ','.join(v.strip() for v in venue if v and v.strip())
        if venue_param:
            url += f"&venue={requests.utils.quote(venue_param)}"
    headers = {'User-Agent': 'ResearchLensAI/1.0 (mailto:research@example.com)'}

    max_attempts = 3
    data = None
    for attempt in range(1, max_attempts + 1):
        resp = requests.get(url, timeout=30, headers=headers)
        if resp.status_code == 429:
            if attempt == max_attempts:
                raise RuntimeError(
                    "rate-limited (429) by Semantic Scholar's public API after "
                    f"{max_attempts} attempts — this app has no API key configured "
                    "for it, so it shares a very low request quota with everyone "
                    "else hitting the unkeyed endpoint. Wait a minute and retry."
                )
            wait_s = float(resp.headers.get('Retry-After', 0)) or (2 ** attempt)
            time.sleep(min(wait_s, 15))
            continue
        resp.raise_for_status()  # raises for any other 4xx/5xx — caller sees the real error
        data = resp.json()
        break

    papers = []
    for item in data.get('data', []):
        title = (item.get('title') or '').strip()
        abstract = (item.get('abstract') or '').strip()
        year = str(item.get('year') or 'N/A')
        authors = [a.get('name', '') for a in (item.get('authors') or [])[:3]]
        # Real Semantic Scholar author IDs (returned by default alongside
        # 'name' in the 'authors' field) for the same top-3 authors — used
        # later by deep_collection.enrich_with_author_influence to look up
        # each author's REAL h-index/citation stats via SS's free Author
        # Batch endpoint. Never invented when SS doesn't return an ID.
        author_ids_ss = [a.get('authorId') for a in (item.get('authors') or [])[:3] if a.get('authorId')]
        if not title:
            continue
        external_ids = item.get('externalIds') or {}
        # Real field — Semantic Scholar's own 'references' list (each with
        # a 'title'), requested via 'references.title' above. Capped to
        # keep payload size sane; used by validate_and_prioritize_gaps to
        # check whether a gap's evidence papers actually cite each other
        # (a real citation-graph signal — "already well-connected
        # literature" vs. "genuinely unconnected work"), never to infer
        # anything about a paper's content.
        reference_titles = [r.get('title', '') for r in (item.get('references') or [])[:100] if r.get('title')]
        papers.append({
            'title': title,
            'abstract': abstract[:500] if abstract else 'No abstract available.',
            'year': year,
            'authors': authors,
            'categories': item.get('publicationTypes') or ['Academic Paper'],
            'source': 'Semantic Scholar',
            'doi': external_ids.get('DOI', ''),
            'venue': item.get('venue', ''),
            'citation_count': item.get('citationCount'),
            'keywords_native': item.get('fieldsOfStudy') or [],
            'reference_titles': reference_titles,
            'author_ids_ss': author_ids_ss,
        })
    return papers


def _fetch_openalex(topic, max_results=50, page=1):
    """
    OpenAlex: broad multidisciplinary + open-access index (250M+ works),
    covers fields ArXiv/Semantic Scholar under-represent (social science,
    humanities, medicine, applied fields). Real papers only, never fake.
    API docs: https://docs.openalex.org

    `page`: OpenAlex's basic (non-cursor) pagination takes a 1-based
    `page` param; OpenAlex documents this as reliable up to roughly the
    first 10,000 matches (page * per_page), which is well past
    deep_collection.py's per-source cap. Also captures `cited_by_count`
    — OpenAlex's own maintained citation count — as `citation_count`.
    """
    try:
        query = requests.utils.quote(topic)
        url = (
            f"https://api.openalex.org/works?search={query}"
            f"&per_page={min(max_results, 100)}&page={max(1, page)}"
            f"&mailto=research@example.com"
        )
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        papers = []
        for item in data.get('results', []):
            title = (item.get('title') or item.get('display_name') or '').strip()
            if not title:
                continue
            abstract = ''
            inv = item.get('abstract_inverted_index')
            if inv:
                positions = {}
                for word, idxs in inv.items():
                    for i in idxs:
                        positions[i] = word
                abstract = ' '.join(positions[i] for i in sorted(positions))
            authorships = item.get('authorships') or []
            authors = [a.get('author', {}).get('display_name', '') for a in authorships[:3]]
            # Real OpenAlex author IDs and their real institution IDs (top
            # 3 authors, top institution per author) — used later by
            # deep_collection.enrich_with_author_influence to look up each
            # author's/institution's REAL summary_stats (h-index,
            # works_count, cited_by_count) via OpenAlex's free Authors and
            # Institutions endpoints. Never invented when an authorship
            # record has no id or no listed institution.
            author_ids_openalex = [
                a.get('author', {}).get('id', '').replace('https://openalex.org/', '')
                for a in authorships[:3] if a.get('author', {}).get('id')
            ]
            institution_ids_openalex = []
            for a in authorships[:3]:
                insts = a.get('institutions') or []
                if insts and insts[0].get('id'):
                    institution_ids_openalex.append(insts[0]['id'].replace('https://openalex.org/', ''))
            concepts = [c.get('display_name', '') for c in (item.get('concepts') or [])[:3]]
            host_venue = (item.get('primary_location') or {}).get('source') or {}
            # Real fields — OpenAlex's own work 'id' and its 'referenced_works'
            # list (OpenAlex IDs of papers this one cites). Used by
            # validate_and_prioritize_gaps to check whether a gap's evidence
            # papers actually cite each other — a real citation-graph signal
            # — never to infer anything about a paper's content.
            openalex_id = (item.get('id') or '').replace('https://openalex.org/', '')
            referenced_works = [
                w.replace('https://openalex.org/', '') for w in (item.get('referenced_works') or [])[:200]
            ]
            papers.append({
                'title': title,
                'abstract': (abstract[:500] if abstract else 'No abstract available.'),
                'year': str(item.get('publication_year') or 'N/A'),
                'authors': authors,
                'categories': concepts or ['Academic Paper'],
                'source': 'OpenAlex',
                'doi': (item.get('doi') or '').replace('https://doi.org/', ''),
                'venue': host_venue.get('display_name', ''),
                'citation_count': item.get('cited_by_count'),
                'openalex_id': openalex_id,
                'referenced_works': referenced_works,
                # OpenAlex's real per-work percentile band (how this
                # work's citation count compares to others published the
                # same year) — a free analogue of a field-weighted
                # citation metric, used honestly under OpenAlex's own
                # field name rather than relabeled as the proprietary
                # Scopus/SciVal "FWCI" metric this app has no access to.
                'citation_percentile_band': item.get('cited_by_percentile_year'),
                'keywords_native': [c.get('display_name', '') for c in (item.get('concepts') or [])[:8]],
                'author_ids_openalex': author_ids_openalex,
                'institution_ids_openalex': institution_ids_openalex,
            })
        return papers
    except Exception as e:
        print(f"OpenAlex fetch failed: {e}")
        return []


def _normalize_title(t):
    return re.sub(r'[^a-z0-9]+', ' ', (t or '').lower()).strip()


def _fetch_arxiv_for_multi(topic, max_results, offset=0):
    """Thin wrapper so arXiv can run through the same thread-pool pattern
    as the other two sources below — raises on failure, same as before.
    `offset`: arXiv's API's own documented `start` param — arXiv has no
    citation-count field, so citation_count is left unset for these papers
    (impact scoring falls back to recency-only for arXiv results)."""
    query = topic.replace(' ', '+')
    url = f"https://export.arxiv.org/api/query?search_query=all:{query}&start={max(0, offset)}&max_results={max_results}&sortBy=submittedDate&sortOrder=descending"
    resp = requests.get(url, timeout=8, headers={'User-Agent': 'ResearchLensAI/1.0 (mailto:research@example.com)'})
    resp.raise_for_status()
    papers = _parse_arxiv_xml(resp.text)
    for p in papers:
        p['source'] = 'arXiv'
    return papers


def fetch_papers_multi_source(topic, max_results=50, sources=None, venue_filter=None):
    """
    Combines ArXiv + Semantic Scholar + OpenAlex into one deduplicated result
    set by default — plus 11 further sources (Crossref, PubMed, Europe PMC,
    DOAJ, ERIC, Internet Archive, INSPIRE-HEP, IEEE Xplore, Springer Nature,
    NASA ADS, CORE — see academic_sources.py) when explicitly requested via
    `sources`. Raises ArxivUnavailableError only if every source queried
    fails.

    `sources`: optional list restricting/expanding which sources to query.
    None or empty means the original default three only (ArXiv, Semantic
    Scholar, OpenAlex) — unchanged behavior for every existing caller that
    doesn't pass this. Pass any of the 14 known source names (including the
    11 from academic_sources.py) to include them instead/as well.

    `venue_filter`: optional list of specific venue/conference names (e.g.
    ['AAAI','NeurIPS','ICML','ICLR'] from the Venues Guide picker). Each
    checked venue is queried directly and independently via DBLP's
    per-venue `streamid:` search (see academic_sources.fetch_dblp_venue)
    — a real, dedicated call per venue, same as Crossref/PubMed/etc.
    below, not a keyword layered on top of a shared general-search API.
    A checked venue with no DBLP stream mapped falls back to Semantic
    Scholar's `venue=` filter param instead of being silently dropped.

    The HTTP calls are independent (different hosts, no shared state) and
    each one blocks on network I/O, which is exactly the case where Python
    threads help despite the GIL — the interpreter releases it while a
    thread is waiting on a socket. Running them concurrently instead of
    one-after-another cuts wall-clock time roughly to that of the slowest
    single source instead of the sum of all of them.
    """
    combined = []
    seen_titles = set()
    errors = []

    def add_papers(papers):
        for p in papers:
            key = _normalize_title(p['title'])
            if key and key not in seen_titles:
                seen_titles.add(key)
                combined.append(p)

    default_sources = {
        'ArXiv': lambda: _fetch_arxiv_for_multi(topic, max_results),
        'Semantic Scholar': lambda: _fetch_semantic_scholar(topic, max_results, venue=venue_filter),
        'OpenAlex': lambda: _fetch_openalex(topic, max_results),
    }
    # `all_known_sources` = default 3 + all 11 extras, used only for lookup
    # when the caller explicitly asks for specific sources by name.
    # `default_sources` (3 only) stays the fallback when `sources` is
    # None/empty, so every pre-existing caller keeps its old behavior.
    all_known_sources = dict(default_sources)
    for name, fn in FREE_SOURCES.items():
        all_known_sources[name] = (lambda fn=fn: fn(topic, max_results))
    for name, fn in KEYED_SOURCES.items():
        all_known_sources[name] = (lambda fn=fn: fn(topic, max_results))
    if sources:
        wanted = {s.strip() for s in sources if s and s.strip()}
        chosen = {name: fn for name, fn in all_known_sources.items() if name in wanted}
        sources_used = chosen or default_sources  # fall back to default 3 if nothing matched
    else:
        sources_used = default_sources

    # STRICT VENUE MODE: when the caller has checked specific venues (e.g.
    # AAAI/NeurIPS/ICML/ICLR), each one is queried directly and
    # independently via its own DBLP `streamid:` search
    # (academic_sources.fetch_dblp_venue) — a real, dedicated call per
    # venue, exactly like Crossref/PubMed/etc. are dedicated calls, not a
    # keyword hint layered on a shared general-search API. ArXiv (no
    # venue concept) and OpenAlex (not venue-filtered here) are excluded
    # so results aren't diluted with unrelated papers. A checked venue
    # with no DBLP stream mapped falls back to Semantic Scholar's
    # `venue=` filter param instead of being silently dropped.
    #
    # DBLP's public endpoint is unauthenticated and reacts badly to more
    # than one request arriving from the same client close together —
    # not just 429s but outright connection resets (RemoteDisconnected),
    # even when the requests are merely *started* a second or two apart
    # through a thread pool (they can still overlap in flight if DBLP is
    # slow to respond). So when a venue filter is active, the DBLP calls
    # are run fully sequentially in this thread — never concurrently —
    # with a real pause between each one, instead of going through the
    # thread pool below at all.
    if venue_filter:
        from academic_sources import DBLP_VENUE_STREAMS, fetch_dblp_venue
        dblp_venues, fallback_venues = [], []
        for v in venue_filter:
            v_clean = (v or '').strip()
            if not v_clean:
                continue
            (dblp_venues if v_clean in DBLP_VENUE_STREAMS else fallback_venues).append(v_clean)

        for i, v in enumerate(dblp_venues):
            if i > 0:
                time.sleep(2.5)  # polite gap — DBLP resets connections on bursts
            name = f'{v} (via DBLP)'
            try:
                papers = fetch_dblp_venue(topic, v, max_results)
                add_papers(papers)
                if papers:
                    print(f"{name} OK: {len(papers)} papers")
                else:
                    errors.append(f"{name}: no results/unreachable")
            except Exception as e:
                errors.append(f"{name}: {e}")
                print(f"{name} failed: {e}")

        if fallback_venues:
            name = 'Semantic Scholar'
            try:
                papers = _fetch_semantic_scholar(topic, max_results, venue=fallback_venues)
                add_papers(papers)
                if papers:
                    print(f"{name} OK: {len(papers)} papers")
                else:
                    errors.append(f"{name}: no results/unreachable")
            except Exception as e:
                errors.append(f"{name}: {e}")
                print(f"{name} failed: {e}")

            # Semantic Scholar's unkeyed tier is the single most
            # rate-limited source this app uses (see _fetch_semantic_scholar's
            # docstring) — for venues with no DBLP stream mapped, it was
            # previously the ONLY source tried, so one 429 sank the whole
            # request even though 15 other real sources exist. OpenAlex has
            # no venue-filter API param, so this fetches by topic and
            # post-filters by venue name below (same substring convention
            # already used for Semantic Scholar's own loose venue= match)
            # — a real, independent second source, not a retry of the same
            # one that just failed.
            name = 'OpenAlex'
            try:
                papers = _fetch_openalex(topic, max_results)
                add_papers(papers)
                if papers:
                    print(f"{name} OK: {len(papers)} papers (pre-venue-filter)")
                else:
                    errors.append(f"{name}: no results/unreachable")
            except Exception as e:
                errors.append(f"{name}: {e}")
                print(f"{name} failed: {e}")

        # DBLP results are already scoped exactly to the requested stream,
        # no post-filtering needed for those. The Semantic Scholar and
        # OpenAlex fallback paths (for venues without a DBLP stream
        # mapped) both need the substring check since neither's venue
        # data is a guaranteed exact-match filter at the API level.
        if fallback_venues:
            wanted_venues = {v.strip().lower() for v in fallback_venues}
            combined = [
                p for p in combined
                if p.get('source') not in ('Semantic Scholar', 'OpenAlex')
                or any(wv in (p.get('venue') or '').lower() for wv in wanted_venues)
            ]
        if not combined:
            venues_label = ', '.join(venue_filter)
            if errors:
                raise ArxivUnavailableError(
                    f"Couldn't fetch results for {venues_label}: {'; '.join(errors)}. "
                    "This is an issue reaching that venue's data source, not a problem "
                    "with your venue selection — wait a moment and retry.",
                    source_label=venues_label,
                )
            raise ArxivUnavailableError(
                f"No papers found for {venues_label} matching this topic. "
                "Try a broader topic, or fewer/different venues.",
                source_label=venues_label,
            )
        return combined[:max_results]

    from academic_sources import KEYED_SOURCE_ENV_VARS
    with ThreadPoolExecutor(max_workers=max(3, len(sources_used))) as pool:
        future_to_name = {pool.submit(fn): name for name, fn in sources_used.items()}
        for future in as_completed(future_to_name):
            name = future_to_name[future]
            try:
                papers = future.result()
                add_papers(papers)
                if papers:
                    print(f"{name} OK: {len(papers)} papers")
                elif name in KEYED_SOURCE_ENV_VARS and not os.environ.get(KEYED_SOURCE_ENV_VARS[name]):
                    errors.append(f"{name}: skipped — {KEYED_SOURCE_ENV_VARS[name]} not set")
                elif name != 'ArXiv':
                    errors.append(f"{name}: no results/unreachable")
            except Exception as e:
                errors.append(f"{name}: {e}")
                print(f"{name} failed in multi-source fetch: {e}")

    if not combined:
        # Label the failure by what was actually queried, so the frontend
        # doesn't blame ArXiv when ArXiv wasn't even one of the selected
        # sources (e.g. the user only checked IEEE Xplore/Crossref).
        if 'ArXiv' in sources_used:
            label = 'ArXiv' if len(sources_used) == 1 else 'your selected sources'
        elif len(sources_used) == 1:
            label = next(iter(sources_used))
        else:
            label = 'your selected sources'
        raise ArxivUnavailableError(
            "Could not reach any academic source. "
            f"Errors: {'; '.join(errors)}. This is likely a network/firewall issue "
            "blocking academic API domains, or the sources you selected all need an "
            "API key that isn't configured — try a different network, check your "
            "selected sources, and retry.",
            source_label=label,
        )

    return combined[:max_results]


def _build_deep_source_registry(context):
    """
    Builds {source_name: callable(topic, max_results=.., **pagination_kwarg)}
    for the deep-collection pipeline (deep_collection.run_two_stage_collection).

    Flowchart stage 2 ("identify sources for the domain"): general-
    purpose sources (arXiv, Semantic Scholar, OpenAlex, Crossref, DOAJ,
    Internet Archive, IEEE Xplore, Springer Nature, ScienceDirect, CORE)
    index across essentially any field and are always included. A
    handful of sources are real, documented SUBJECT-SPECIFIC databases
    (PubMed/Europe PMC for biomedical literature, ERIC for education
    research, INSPIRE-HEP/NASA ADS for physics/astrophysics) — these are
    only included when the caller's domain/sub-domain/keywords/topic/
    research-question (and, if it ran, the stage-1 DomainUnderstandingAgent's
    primary_domain/sub_domains/key_terminology) actually match that
    source's documented scope (see academic_sources.SPECIALIZED_SOURCE_DOMAINS).
    If nothing matches any specialized source — an ambiguous or
    cross-cutting domain — the safe default is to include ALL of them
    rather than risk silently under-collecting; this is a real
    relevance filter, not a hard allowlist. An explicit
    context['paper_sources'] always overrides this entirely (the
    caller's explicit choice, same convention as fetch_papers_multi_source),
    and any keyed source with no API key configured in this deployment
    is silently skipped, never faked.

    context['venue_filter'] (any of AAAI/NeurIPS/ICML/ICLR) adds one
    dedicated DBLP-backed entry per checked venue on top of whatever
    general sources are also selected, so a strict-venue paper still
    competes on impact score against the general-source papers in
    Stage 2, instead of the two modes being mutually exclusive the way
    fetch_papers_multi_source's single-shot venue mode is.
    """
    requested = context.get('paper_sources')
    venue_filter = context.get('venue_filter')

    always_general = {
        'ArXiv': _fetch_arxiv_for_multi,
        'Semantic Scholar': _fetch_semantic_scholar,
        'OpenAlex': _fetch_openalex,
    }
    # Free sources that AREN'T subject-specific stay always-on too;
    # only the ones actually listed in SPECIALIZED_SOURCE_DOMAINS get
    # domain-gated below.
    for name, fn in FREE_SOURCES.items():
        if name not in SPECIALIZED_SOURCE_DOMAINS:
            always_general[name] = fn
    keyed_status = keyed_source_status()
    for name, fn in KEYED_SOURCES.items():
        if keyed_status.get(name):
            always_general[name] = fn  # none of the current keyed sources are domain-specific

    registry = {}
    if requested:
        wanted = {s.strip() for s in requested if s and s.strip()}
        all_general = dict(always_general)
        for name, fn in FREE_SOURCES.items():
            if name in SPECIALIZED_SOURCE_DOMAINS:
                all_general[name] = fn  # explicit request can still name a specialized source directly
        for name, fn in all_general.items():
            if name in wanted:
                registry[name] = fn
        if not registry:
            registry = dict(all_general)  # fall back to everything, same convention as fetch_papers_multi_source
        context['deep_sources_skipped_by_domain'] = []  # explicit selection bypasses domain filtering entirely
    else:
        registry = dict(always_general)

        du = context.get('deep_domain_understanding') or {}
        domain_text = ' '.join(str(v) for v in [
            context.get('domain'), context.get('sub_domain'), context.get('topic'),
            context.get('research_question'),
            ' '.join(context.get('keywords') or []) if isinstance(context.get('keywords'), list) else context.get('keywords'),
            du.get('primary_domain'), ' '.join(du.get('sub_domains') or []),
            ' '.join(du.get('key_terminology') or []),
        ] if v)
        relevant = relevant_specialized_sources(domain_text)
        skipped = []
        for name, fn in FREE_SOURCES.items():
            if name not in SPECIALIZED_SOURCE_DOMAINS:
                continue
            if not relevant or name in relevant:
                # `not relevant` = nothing matched any specialized
                # source for this domain text — ambiguous, so include
                # all of them rather than risk under-collecting.
                registry[name] = fn
            else:
                skipped.append(name)
        context['deep_sources_skipped_by_domain'] = skipped

    if venue_filter:
        for v in venue_filter:
            v = (v or '').strip()
            if v and v in DBLP_VENUE_STREAMS:
                registry[f'{v} (via DBLP)'] = (
                    lambda topic, max_results=100, first=0, _v=v:
                        fetch_dblp_venue(topic, _v, max_results=max_results, first=first)
                )
            # A checked venue with no DBLP stream mapped has no dedicated
            # entry here — Semantic Scholar's general entry above still
            # covers it via a normal keyword search, just not scoped to
            # that exact venue the way DBLP's streamid: search is.
    return registry


class ArxivUnavailableError(Exception):
    """Raised when no requested academic source could be reached — no fake
    data is ever substituted. `source_label` names what actually failed
    (e.g. 'Semantic Scholar', 'IEEE Xplore', 'ArXiv') so the frontend can
    show an accurate error instead of always blaming ArXiv."""
    def __init__(self, message, source_label='ArXiv'):
        super().__init__(message)
        self.source_label = source_label
    pass

def _parse_arxiv_xml(xml_text):
    root = ET.fromstring(xml_text)
    ns = {'atom': 'http://www.w3.org/2005/Atom', 'arxiv': 'http://arxiv.org/schemas/atom'}
    papers = []
    for entry in root.findall('atom:entry', ns):
        title   = entry.find('atom:title', ns)
        summary = entry.find('atom:summary', ns)
        published = entry.find('atom:published', ns)
        authors = entry.findall('atom:author', ns)
        categories = entry.findall('atom:category', ns)
        id_el = entry.find('atom:id', ns)
        author_names = [a.find('atom:name', ns).text for a in authors if a.find('atom:name', ns) is not None]
        cat_terms = [c.get('term', '') for c in categories]
        # arXiv papers usually have no DOI of their own, but the <id> URL
        # (e.g. http://arxiv.org/abs/2301.12345v1) always carries the
        # arXiv ID, which deep_collection.backfill_citation_counts uses
        # as a fallback lookup key against Semantic Scholar's real
        # citation data when a paper has no DOI to look up by instead.
        arxiv_id = ''
        if id_el is not None and id_el.text:
            m = re.search(r'abs/([\w.\-/]+?)(v\d+)?$', id_el.text.strip())
            if m:
                arxiv_id = m.group(1)
        # Real, optional fields from arXiv's own Atom extension namespace:
        # <arxiv:doi> — present only for the minority of preprints where
        # the author or arXiv itself registered a DOI (e.g. via a journal
        # publication); <arxiv:journal_ref> — a free-text citation the
        # AUTHOR entered for where the paper was later published, present
        # only when they've filled it in. Neither is guessed when absent.
        doi_el = entry.find('arxiv:doi', ns)
        journal_ref_el = entry.find('arxiv:journal_ref', ns)
        if title is not None and summary is not None:
            papers.append({
                'title': title.text.strip().replace('\n', ' '),
                'abstract': summary.text.strip().replace('\n', ' ')[:500],
                'year': published.text[:4] if published is not None else 'N/A',
                'authors': author_names[:3],
                'categories': cat_terms[:3],
                'source': 'arXiv',
                'arxiv_id': arxiv_id,
                'doi': (doi_el.text or '').strip() if doi_el is not None and doi_el.text else '',
                'venue': (journal_ref_el.text or '').strip() if journal_ref_el is not None and journal_ref_el.text else '',
            })
    return papers

def fetch_arxiv_papers(topic, max_results=50):
    """
    Fetches REAL papers from ArXiv only. Tries both the export.arxiv.org and
    arxiv.org endpoints (export has known intermittent reliability issues),
    with retries and backoff. Never falls back to synthetic/demo data —
    if ArXiv truly cannot be reached, raises ArxivUnavailableError so the
    caller can surface an honest error to the user instead of faking results.
    """
    query = topic.replace(' ', '+')
    endpoints = [
        f"https://export.arxiv.org/api/query?search_query=all:{query}&start=0&max_results={max_results}&sortBy=submittedDate&sortOrder=descending",
        f"http://export.arxiv.org/api/query?search_query=all:{query}&start=0&max_results={max_results}&sortBy=submittedDate&sortOrder=descending",
    ]
    last_error = None
    for endpoint_idx, url in enumerate(endpoints):
        for attempt in range(2):  # 2 attempts per endpoint — faster failover to Semantic Scholar
            try:
                resp = requests.get(url, timeout=8, headers={'User-Agent': 'ResearchLensAI/1.0 (mailto:research@example.com)'})
                resp.raise_for_status()
                papers = _parse_arxiv_xml(resp.text)
                if papers:
                    print(f"ArXiv OK: {len(papers)} real papers fetched (endpoint {endpoint_idx+1}, attempt {attempt+1})")
                    return papers
                last_error = "ArXiv returned 0 results for this query"
            except Exception as e:
                last_error = str(e)
                print(f"ArXiv endpoint {endpoint_idx+1}, attempt {attempt+1} failed: {e}")
                time.sleep(1)  # short pause then try next
    # ArXiv failed — try Semantic Scholar as real backup (200M+ real papers, different domain)
    print(f"ArXiv unavailable ({last_error}) — trying Semantic Scholar as backup source...")
    try:
        semantic_papers = _fetch_semantic_scholar(topic, max_results)
    except Exception as e:
        semantic_papers = []
        last_error = f"{last_error}; Semantic Scholar backup also failed: {e}"
    if semantic_papers:
        print(f"Semantic Scholar backup succeeded: {len(semantic_papers)} real papers fetched")
        return semantic_papers
    raise ArxivUnavailableError(
        f"Could not reach ArXiv or Semantic Scholar. Both academic paper sources "
        f"failed — last error: {last_error}. This is a network/firewall issue on your "
        f"machine blocking academic API domains. Switch to a mobile hotspot and retry."
    )

def extract_pdf_text(filepath):
    """Returns page-tagged text (for the LLM to cite [PAGE n] against) plus
    the plain concatenated text (for keyword extraction + quote verification)."""
    tagged = ""
    plain = ""
    try:
        with open(filepath, 'rb') as f:
            reader = PyPDF2.PdfReader(f)
            for i, page in enumerate(reader.pages[:20], 1):
                page_text = page.extract_text() or ""
                tagged += f"\n[PAGE {i}]\n{page_text}"
                plain += page_text
        return tagged[:9000], plain[:8000]
    except Exception as e:
        print(f"PDF error: {e}")
        return "", ""

def _verify_quote_in_text(quote, plain_text):
    """
    Checks a claimed evidence quote actually appears in the source text
    (normalized for whitespace/case) before trusting it — turns
    'evidence_quote' into something checked, not just another thing the
    model could hallucinate.
    """
    if not quote or not plain_text:
        return False
    norm_quote = re.sub(r'\s+', ' ', quote).strip().lower()
    norm_text = re.sub(r'\s+', ' ', plain_text).lower()
    return len(norm_quote) > 8 and norm_quote in norm_text

# ─── Keywords ───────────────────────────────────────────────────────
STOPWORDS = set(['the','a','an','and','or','but','in','on','at','to','for','of','with','is','are','was','were','be','been','have','has','had','do','does','did','will','would','could','should','may','might','this','that','these','those','from','by','as','not','it','its','we','our','their','they','he','she','his','her','you','your','i','my','me','us','them','what','which','who','how','when','where','why','if','then','than','so','yet','both','either','each','all','any','more','most','such','into','through','during','before','after','above','below','between','out','off','over','under','again','further','also','only','just','very','too','can','paper','study','research','using','based','model','data','results','proposed','method','approach','used','show','shown','present','provide','new','different','two','one','three','however','therefore','thus','hence','while','although','since','because','use','used','uses','well','often','many','much','other','same','first','second','third','large','small','high','low','good','better','best','important','significant','various','several','multiple','set','task','due','per','even','among','within','without','along','across','around'])

def extract_keywords(text, top_n=30):
    words = re.findall(r'\b[a-z]{4,}\b', text.lower())
    filtered = [w for w in words if w not in STOPWORDS]
    bigrams = [filtered[i]+' '+filtered[i+1] for i in range(len(filtered)-1)]
    counts = Counter(filtered) + Counter(bigrams)
    return [w for w, _ in counts.most_common(top_n)]

# ─── Knowledge graph ─────────────────────────────────────────────────
# Builds a real multi-entity graph (papers, authors, keywords, AI-identified
# gaps, emerging trends) with genuine analytics (PageRank + community
# detection via networkx) and a structural gap-discovery heuristic.
# Does NOT include Node2Vec/GNN link-prediction or cross-paper entity
# resolution — those need a training pipeline and much larger corpus than
# a single on-demand analysis has.
def build_knowledge_graph(keywords, papers, analysis=None):
    G = nx.Graph()
    for kw in keywords[:20]:
        G.add_node(kw, type='keyword')

    for p in papers[:12]:
        short = p['title'][:40]+'...' if len(p['title'])>40 else p['title']
        G.add_node(short, type='paper', year=p.get('year','N/A'), source=p.get('source',''))
        p_words = extract_keywords(p['title']+' '+p['abstract'], 10)
        for kw in keywords[:15]:
            if kw in p_words or any(kw in pw for pw in p_words):
                G.add_edge(short, kw, weight=1, relation='belongs_to')
        for author in (p.get('authors') or [])[:2]:
            author = (author or '').strip()
            if len(author) > 2:
                G.add_node(author, type='author')
                G.add_edge(short, author, weight=1, relation='written_by')

    for i, k1 in enumerate(keywords[:15]):
        for k2 in keywords[i+1:15]:
            shared = sum(1 for p in papers if k1 in p['abstract'].lower() and k2 in p['abstract'].lower())
            if shared > 0:
                G.add_edge(k1, k2, weight=shared, relation='related_to')

    # AI-derived nodes: ground the graph in the LLM's own findings so gap
    # and trend nodes are real analysis output, not decorative extras
    if analysis:
        for gap in analysis.get('research_gaps', [])[:10]:
            gtitle = (gap.get('gap_title') or '')[:45]
            if not gtitle:
                continue
            G.add_node(gtitle, type='gap', severity=gap.get('severity',''))
            for ev_title in gap.get('evidence_papers', [])[:3]:
                ev_key = (ev_title or '').strip().lower()[:20]
                if not ev_key:
                    continue
                for p in papers[:12]:
                    if ev_key in p['title'].lower():
                        short = p['title'][:40]+'...' if len(p['title'])>40 else p['title']
                        G.add_edge(gtitle, short, weight=1, relation='identified_in')
                        break
            gap_words = set(extract_keywords(gtitle, 6))
            for kw in keywords[:15]:
                if kw in gap_words:
                    G.add_edge(gtitle, kw, weight=1, relation='relates_to')

        for trend in analysis.get('emerging_trends', [])[:6]:
            tname = (trend.get('trend') or '')[:40]
            if not tname:
                continue
            G.add_node(tname, type='trend')
            trend_words = set(extract_keywords(tname+' '+(trend.get('description') or ''), 6))
            for kw in keywords[:15]:
                if kw in trend_words:
                    G.add_edge(tname, kw, weight=1, relation='related_to')

    return G

def _graph_analytics(G):
    """PageRank (influence) + greedy-modularity community detection."""
    if G.number_of_nodes() == 0:
        return {}, {}
    try:
        pagerank = nx.pagerank(G, weight='weight') if G.number_of_edges() > 0 else {n: 1/G.number_of_nodes() for n in G.nodes()}
    except Exception:
        pagerank = {n: 0 for n in G.nodes()}
    community_map = {}
    try:
        communities = list(nx.algorithms.community.greedy_modularity_communities(G, weight='weight'))
        for idx, comm in enumerate(communities):
            for n in comm:
                community_map[n] = idx
    except Exception:
        pass
    return pagerank, community_map

def discover_graph_gaps(G):
    """
    Structural gap discovery: finds keyword pairs that are each individually
    well-connected (well-studied within the fetched literature) but never
    co-occur in any single fetched paper — a graph-structure signal for an
    unexplored combination, complementary to the LLM's own gap list.
    """
    suggestions = []
    kw_nodes = [n for n, d in G.nodes(data=True) if d.get('type') == 'keyword']
    degrees = dict(G.degree(kw_nodes))
    well_studied = sorted(kw_nodes, key=lambda n: -degrees.get(n, 0))[:10]
    for i, k1 in enumerate(well_studied):
        for k2 in well_studied[i+1:]:
            if degrees.get(k1, 0) >= 2 and degrees.get(k2, 0) >= 2 and not G.has_edge(k1, k2):
                suggestions.append({
                    'topic_a': k1,
                    'topic_b': k2,
                    'reason': f'"{k1}" and "{k2}" are each independently well-studied in the fetched literature, but no fetched paper connects both — a potential unexplored combination.'
                })
            if len(suggestions) >= 5:
                return suggestions
    return suggestions

def graph_to_json(G):
    pagerank, community_map = _graph_analytics(G)
    nodes = [{
        'id': n,
        'type': d.get('type', 'keyword'),
        'degree': G.degree(n),
        'year': d.get('year', ''),
        'severity': d.get('severity', ''),
        'source': d.get('source', ''),
        'pagerank': round(pagerank.get(n, 0), 4),
        'community': community_map.get(n, -1)
    } for n, d in G.nodes(data=True)]
    edges = [{
        'source': u, 'target': v,
        'weight': d.get('weight', 1),
        'relation': d.get('relation', 'related_to')
    } for u, v, d in G.edges(data=True)]
    return {'nodes': nodes, 'edges': edges}

# ─── Single paper Groq analysis ──────────────────────────────────────
def _log_novelty(caller, topic, novelty):
    """
    Prints the exact real evidence behind a novelty score to stdout, in
    the same convention as the multi-source fetch log lines above
    (`Crossref OK: 100 papers`, `ArXiv failed in multi-source fetch: ...`)
    — so "why is this score X" is answerable straight from the server
    log for THIS run, not something that has to be reconstructed after
    the fact or guessed at. Never fabricates an explanation: if there
    was no comparable paper at all, that's stated plainly instead of
    inventing a reason.
    """
    score = novelty.get('novelty_score')
    n = novelty.get('novelty_compared_against', 0)
    scoped = novelty.get('novelty_domain_scoped')
    closest = novelty.get('novelty_closest_matches') or []
    if score is None:
        print(f"Novelty ({caller}, topic='{topic}'): no comparable papers were collected — score not computed.")
        return
    scope_note = "domain-scoped" if scoped else "full corpus"
    if not closest:
        print(f"Novelty ({caller}, topic='{topic}'): {score}/100 — {n} papers compared ({scope_note}), "
              f"zero textual overlap with any of them.")
    else:
        top = closest[0]
        print(f"Novelty ({caller}, topic='{topic}'): {score}/100 — {n} papers compared ({scope_note}). "
              f"Closest match: \"{top.get('title')}\" ({top.get('year')}, {top.get('source')}) "
              f"at {top.get('similarity_pct')}% similarity.")


# ─── Token-budget batching for large paper sets ───────────────────────
# Groq's free/on_demand tier caps openai/gpt-oss-120b (our GROQ_MODEL
# default) at 8,000 TPM regardless of which model is picked — that cap
# is per-model on Groq's side, not something a bigger context window
# fixes. A 1000-paper title list alone runs ~15-20K tokens, so a single
# call is not reliable no matter which chat model is configured. These
# helpers split any paper list into chunks that fit the configured
# budget, run one Groq call per chunk, and merge the resulting gaps —
# this is what actually lets the pipeline "analyze 1000 papers" instead
# of silently truncating to whatever fits in one prompt.
GROQ_TPM_BUDGET = int(os.environ.get('GROQ_TPM_BUDGET', '8000'))
_CHARS_PER_TOKEN = 4  # rough, conservative estimate for English text

def _chunk_papers_by_token_budget(papers, line_fn, reserved_tokens=4200, overhead_tokens=600):
    """
    Splits `papers` into chunks whose rendered title-lines (via line_fn)
    stay under (GROQ_TPM_BUDGET - reserved_tokens - overhead_tokens)
    input tokens. `reserved_tokens` leaves headroom for the completion
    (max_tokens) since Groq's TPM counts prompt + completion together;
    `overhead_tokens` covers the fixed prompt scaffolding text.
    """
    budget = max(500, GROQ_TPM_BUDGET - reserved_tokens - overhead_tokens)
    chunk, chunk_tokens = [], 0
    for p in papers:
        line = line_fn(p)
        line_tokens = max(1, len(line) // _CHARS_PER_TOKEN)
        if chunk and chunk_tokens + line_tokens > budget:
            yield chunk
            chunk, chunk_tokens = [], 0
        chunk.append(p)
        chunk_tokens += line_tokens
    if chunk:
        yield chunk

def _dedupe_gaps(gaps, max_gaps=15):
    seen, out = set(), []
    for g in gaps:
        key = re.sub(r'[^a-z0-9]+', ' ', (g.get('gap_title') or '').lower()).strip()[:60]
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(g)
    return out[:max_gaps]


def _salvage_truncated_json(raw):
    """
    Best-effort recovery for a Groq response that got cut off mid-object
    by max_tokens (Groq's error/log signature: "Unterminated string
    starting at..."). Rather than discarding the whole batch's gaps,
    this walks backward through every '}' in the raw text, and for each
    one checks whether everything up to that point is a valid JSON
    prefix once the still-open brackets are closed — i.e. it trims back
    to the last FULLY-WRITTEN object (existing_coverage entry, gap
    entry, etc.) and discards only the one that was mid-write. Returns
    a parsed dict with whatever complete data survives, or None if
    nothing usable does.
    """
    close_positions = [m.end() for m in re.finditer(r'\}', raw)]
    for end in reversed(close_positions):
        candidate = raw[:end]
        opens = candidate.count('{') - candidate.count('}')
        arr_opens = candidate.count('[') - candidate.count(']')
        if opens < 0 or arr_opens < 0:
            continue
        attempt = re.sub(r',\s*$', '', candidate.rstrip())
        attempt += ']' * arr_opens + '}' * opens
        try:
            return json.loads(attempt)
        except Exception:
            continue
    return None


def _parse_groq_json(raw, log_prefix):
    raw = raw.strip()
    raw = re.sub(r'^```[a-z]*\n?', '', raw)
    raw = re.sub(r'\n?```$', '', raw)
    raw = re.sub(r'\\(?!["\\bfnrtu/]|u[0-9a-fA-F]{4})', '', raw)
    try:
        return json.loads(raw)
    except Exception as e:
        salvaged = _salvage_truncated_json(raw)
        if salvaged is not None:
            print(f"[{log_prefix}] response was truncated by max_tokens ({e}) — "
                  f"salvaged {len(salvaged.get('research_gaps', []))} complete gap(s) "
                  f"from before the cutoff instead of discarding the whole batch")
            return salvaged
        print(f"[{log_prefix}] skipped a batch after bad JSON: {e}")
        return None


def analyze_gaps_with_groq(api_key, topic, pdf_text, papers, keywords, primary_domain=None):
    client = Groq(api_key=api_key)
    kw_str = ", ".join(keywords[:20])
    pdf_excerpt = pdf_text[:3000] if pdf_text else "No PDF provided."
    batches = list(_chunk_papers_by_token_budget(papers, lambda p: f"- {p['title']} ({p['year']})"))
    merged = {"research_gaps": [], "existing_coverage": [], "emerging_trends": []}
    got_headline_fields = False
    succeeded = 0
    for i, batch in enumerate(batches):
        result = _analyze_gaps_batch_with_groq(client, topic, pdf_excerpt, batch, kw_str)
        if result is None:
            continue
        succeeded += 1
        if not got_headline_fields:
            merged["summary"] = result.get("summary", "")
            merged["recommended_next_steps"] = result.get("recommended_next_steps", [])
            merged["gap_density"] = result.get("gap_density", "medium")
            merged["overall_recommendation"] = result.get("overall_recommendation", "")
            got_headline_fields = True
        merged["research_gaps"].extend(result.get("research_gaps", []))
        merged["existing_coverage"].extend(result.get("existing_coverage", []))
        merged["emerging_trends"].extend(result.get("emerging_trends", []))
        if i < len(batches) - 1:
            time.sleep(2.5)  # stay under the 30 RPM free-tier request cap
    if succeeded == 0:
        raise RuntimeError(
            f"Gap analysis failed on all {len(batches)} batch(es) — every Groq call "
            f"either errored or returned bad JSON. Check the [gap-analysis-batch] log "
            f"lines above for the underlying Groq error."
        )
    merged["research_gaps"] = _dedupe_gaps(merged["research_gaps"])
    merged["existing_coverage"] = merged["existing_coverage"][:6]
    merged["emerging_trends"] = merged["emerging_trends"][:6]
    result = merged
    _finish_gap_analysis_with_novelty(result, pdf_text, topic, papers, primary_domain)
    return result


def _analyze_gaps_batch_with_groq(client, topic, pdf_excerpt, papers, kw_str):
    paper_titles = "\n".join([f"- {p['title']} ({p['year']})" for p in papers])
    prompt = f"""You are an expert academic research analyst. Analyze the following and provide a comprehensive research gap analysis.

RESEARCH TOPIC: {topic}
USER'S PAPER EXCERPT: {pdf_excerpt}
EXISTING RELATED PAPERS:
{paper_titles}
KEY CONCEPTS FOUND: {kw_str}

IMPORTANT: Based on the related papers and the topic provided, identify BETWEEN 3 AND 6 distinct, non-overlapping research gaps from THIS batch of papers (results from other batches of the same corpus are merged separately, so keep this batch's response focused and complete rather than exhaustive). Each gap must be genuinely different from the others, covering a different sub-theme, methodology, application domain, or limitation.

Provide a detailed JSON response (no markdown, pure JSON):
{{
  "summary": "2-3 sentence overview",
  "existing_coverage": [
    {{"area": "area name", "coverage": "high/medium/low", "description": "brief desc"}},
    {{"area": "area name", "coverage": "high/medium/low", "description": "brief desc"}},
    {{"area": "area name", "coverage": "high/medium/low", "description": "brief desc"}}
  ],
  "research_gaps": [
    {{
      "gap_title": "Specific, distinct gap title (do not repeat themes across gaps)",
      "severity": "critical/major/moderate",
      "description": "Detailed description",
      "why_important": "Why filling this gap matters",
      "suggested_approach": "How to address this gap",
      "estimated_impact": "Expected contribution",
      "evidence_papers": ["actual paper title from the list above", "another actual paper title"],
      "evidence_explanation": "Detailed explanation of HOW these specific papers prove this gap exists - cite what they found, what they explicitly said is missing or future work",
      "evidence_quote": "If (and only if) the USER'S PAPER EXCERPT above directly supports this gap, copy an EXACT short quote (max 25 words) verbatim from it. If no exact supporting quote exists in the excerpt, use an empty string - do NOT paraphrase or invent one.",
      "evidence_page": "The [PAGE n] number the quote appeared under in the excerpt, as an integer, or null if evidence_quote is empty",
      "confidence_score": 85
    }}
    // ... continue this pattern for EVERY distinct gap you find, do not stop at a fixed number
  ],
  "emerging_trends": [
    {{"trend": "trend name", "description": "brief description", "relevance": "high/medium"}},
    {{"trend": "trend name", "description": "brief description", "relevance": "high/medium"}},
    {{"trend": "trend name", "description": "brief description", "relevance": "high/medium"}}
  ],
  "recommended_next_steps": ["Step 1","Step 2","Step 3","Step 4","Step 5"],
  "gap_density": "high/medium/low",
  "overall_recommendation": "One powerful sentence"
}}"""
    try:
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role":"user","content":prompt}],
            temperature=0.3, max_tokens=4200
        )
    except Exception as e:
        # A single batch hitting a rate limit (or any transient Groq
        # error) shouldn't fail the whole 1000-paper run — the caller
        # just merges whatever batches succeeded.
        print(f"[gap-analysis-batch] skipped a batch after Groq error: {e}")
        return None
    return _parse_groq_json(response.choices[0].message.content, "gap-analysis-batch")


def _finish_gap_analysis_with_novelty(result, pdf_text, topic, papers, primary_domain):
    # Real, computed novelty score — see agents/textsim.compute_grounded_novelty.
    # Never the LLM's own self-reported guess: the subject being scored
    # is the uploaded paper's real text (or, with no PDF, the topic +
    # idea description actually entered), compared via TF-IDF against
    # the real papers this run collected, scoped to `primary_domain`
    # when one was classified/entered.
    subject_text = pdf_text if pdf_text and pdf_text.strip() else topic
    novelty = compute_grounded_novelty(subject_text, papers, domain_terms=[primary_domain] if primary_domain else None)
    _log_novelty("analyze_gaps_with_groq", topic, novelty)
    result.update(novelty)
    return result

# ─── Multi-paper Groq analysis ───────────────────────────────────────
def analyze_multiple_papers_with_groq(api_key, topic, papers_data, arxiv_papers, keywords, primary_domain=None):
    client = Groq(api_key=api_key)
    papers_summary = ""
    for i, pd in enumerate(papers_data, 1):
        papers_summary += f"\nPAPER {i}: {pd['name']}\n{pd['text'][:1500]}\n---"
    arxiv_titles = "\n".join([f"- {p['title']} ({p['year']})" for p in arxiv_papers[:8]])
    prompt = f"""You are an expert academic research analyst. Analyze these {len(papers_data)} uploaded research papers together.

RESEARCH TOPIC: {topic}

UPLOADED PAPERS:
{papers_summary}

RELATED PUBLISHED PAPERS FROM ARXIV:
{arxiv_titles}

KEY CONCEPTS: {', '.join(keywords[:20])}

IMPORTANT: Based on the uploaded papers and related literature, identify BETWEEN 5 AND 8 distinct, non-overlapping research gaps. Do not artificially limit yourself to a small fixed number. Each gap must be genuinely different, covering a different sub-theme, methodology, application domain, or limitation found across the papers.

Provide a comprehensive multi-paper analysis as pure JSON (no markdown):
{{
  "summary": "2-3 sentence overview of what all papers together cover",
  "literature_review": {{
    "introduction": "2-3 sentence introduction paragraph about this research area",
    "main_findings": "3-4 sentence paragraph summarizing what these papers collectively found",
    "methodologies": "2-3 sentence paragraph about the methods used across papers",
    "limitations": "2-3 sentence paragraph about limitations found across papers",
    "conclusion": "2-3 sentence conclusion paragraph"
  }},
  "paper_summaries": [
    {{
      "paper_name": "filename of paper",
      "objective": "Main objective of this paper",
      "methodology": "Methods/techniques used",
      "dataset": "Dataset used if mentioned",
      "key_results": "Key findings and results",
      "limitations": "Limitations mentioned"
    }}
  ],
  "comparative_analysis": [
    {{
      "aspect": "Aspect being compared (e.g. Methodology)",
      "comparisons": [
        {{"paper": "Paper name", "detail": "What this paper does for this aspect"}}
      ]
    }}
  ],
  "research_gaps": [
    {{
      "gap_title": "Specific, distinct gap title (each gap must cover a different theme, do not repeat)",
      "severity": "critical/major/moderate",
      "description": "Detailed description of the gap",
      "why_important": "Why filling this gap matters",
      "suggested_approach": "How to address this gap",
      "estimated_impact": "Expected contribution to the field",
      "evidence_papers": ["actual paper name from the list above", "another actual paper name"],
      "evidence_explanation": "Detailed explanation citing specific sections/findings from the uploaded papers that PROVE this gap exists. Reference what each paper found and what it explicitly says is missing or future work.",
      "confidence_score": 88
    }}
    // ... continue this pattern for EVERY distinct gap found, do not stop at a fixed number
  ],
  "emerging_trends": [
    {{"trend": "trend name", "description": "description", "relevance": "high/medium"}},
    {{"trend": "trend name", "description": "description", "relevance": "high/medium"}},
    {{"trend": "trend name", "description": "description", "relevance": "high/medium"}}
  ],
  "recommended_next_steps": ["Step 1","Step 2","Step 3","Step 4","Step 5"],
  "gap_density": "high/medium/low",
  "overall_recommendation": "One powerful sentence"
}}"""
    response = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[{"role":"user","content":prompt}],
        temperature=0.3, max_tokens=6000
    )
    raw = response.choices[0].message.content.strip()
    raw = re.sub(r'^```[a-z]*\n?','',raw)
    raw = re.sub(r'\n?```$','',raw)
    # Clean bad escapes the LLM sometimes produces
    import re as _re2
    raw = _re2.sub(r'\\(?!["\\bfnrtu/]|u[0-9a-fA-F]{4})', '', raw)
    result = json.loads(raw)

    # Real, computed novelty score — see analyze_gaps_with_groq above for
    # the full rationale. Subject text here is the combined text of all
    # uploaded papers (what's actually being assessed for novelty as a
    # submission), compared against the real arxiv_papers this run
    # collected, scoped to `primary_domain` when available.
    subject_text = " ".join((pd.get('text') or '')[:1500] for pd in papers_data)
    novelty = compute_grounded_novelty(subject_text, arxiv_papers, domain_terms=[primary_domain] if primary_domain else None)
    _log_novelty("analyze_multiple_papers_with_groq", topic, novelty)
    result.update(novelty)
    return result

# ─── Deep-collection trend/gap analysis ──────────────────────────────
# ─── Domain understanding (query expansion) ──────────────────────────
def analyze_domain_understanding_with_groq(api_key, topic, domain=None, sub_domain=None,
                                             keywords=None, research_question=None):
    """
    Flowchart stage 1. Turns the user's raw input (domain/sub-domain/
    topic/keywords/research question) into a small set of REAL search
    query strings that actually get sent to the academic-source APIs in
    deep_collection.py — this is not a cosmetic "understanding" summary
    with no effect on collection. The LLM only proposes query TEXT;
    every query it proposes is a genuine search string handed to a real
    API in Stage 0, and every paper that comes back from an expansion
    query is a real collected paper subject to the exact same impact
    ranking as everything else. Nothing about any paper's content is
    inferred by this step — only which search strings get tried.
    """
    client = Groq(api_key=api_key)
    context_lines = [f"Research topic: {topic}"]
    if domain:
        context_lines.append(f"Domain: {domain}")
    if sub_domain:
        context_lines.append(f"Sub-domain: {sub_domain}")
    if keywords:
        kw = ', '.join(keywords) if isinstance(keywords, list) else keywords
        context_lines.append(f"Keywords: {kw}")
    if research_question:
        context_lines.append(f"Research question: {research_question}")
    context_block = "\n".join(context_lines)

    prompt = f"""You are a research librarian scoping a literature search.

{context_block}

Identify the primary domain and sub-domain this falls under, extract the most important keywords/terminology, and propose 3-5 alternative SEARCH QUERY STRINGS (short strings, 2-6 words each, suitable for searching academic databases like arXiv/Semantic Scholar/PubMed directly) that would surface papers a literal search for "{topic}" alone might miss — synonyms, closely related sub-problems, or terminology a different research community uses for the same underlying idea. These are query strings to be searched, not explanations.

Respond with pure JSON, no markdown:
{{
  "primary_domain": "...",
  "sub_domains": ["...", "..."],
  "key_terminology": ["...", "..."],
  "expanded_queries": ["query one", "query two", "query three"]
}}"""
    response = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3, max_tokens=800,
    )
    raw = response.choices[0].message.content.strip()
    raw = re.sub(r'^```[a-z]*\n?', '', raw)
    raw = re.sub(r'\n?```$', '', raw)
    return json.loads(raw)


def forecast_research_trends_with_groq(api_key, topic, trend_stats, current_trends=None):
    """
    Flowchart stage 16 (Research Trend Forecasting), as its own explicit
    LLM call — separate from gap detection/validation/prioritization
    (stages 17-19) — so "past -> present -> future" forecasting happens
    once, on its own, and its output is then handed to gap detection as
    an input rather than the two being blended into a single call.

    Grounded in the same real signals as gap detection: keyword_trend_stats
    (rising/entrenched/declining/growth-rate/co-occurrence, all real
    counts from the collected corpus — see deep_collection.keyword_trend_stats)
    plus, when available, real live-search current_trends (flowchart
    stage 15). Returns a JSON dict with the forecast; never touches
    individual papers or proposes named gaps itself — that stays stage
    17's job in analyze_deep_research_gaps_with_groq below, which
    receives this function's output as additional context.
    """
    client = Groq(api_key=api_key)
    rising = ", ".join(trend_stats.get('rising_keywords', [])[:20]) or "none detected"
    entrenched = ", ".join(trend_stats.get('entrenched_keywords', [])[:20]) or "none detected"
    declining = ", ".join(trend_stats.get('declining_keywords', [])[:15]) or "none detected"
    growth = trend_stats.get('growth_rate', {})
    fastest_growing = ", ".join(
        f"{term} ({rate:+.0%})" for term, rate in sorted(growth.items(), key=lambda kv: kv[1], reverse=True)[:10]
    ) or "not enough year-over-year data"
    clusters = trend_stats.get('keyword_clusters', [])[:8]
    clusters_str = "; ".join("+".join(c) for c in clusters) or "none notable"
    categories = trend_stats.get('keyword_categories', {})
    categories_str = "; ".join(
        f"{cat}: {', '.join(terms[:8])}" for cat, terms in categories.items() if terms
    ) or "none categorized"

    current_trends_block = ""
    if current_trends:
        trend_lines = "\n".join(
            f"- {r.get('title', '')}: {r.get('snippet', '')[:300]}" for r in current_trends[:6]
        )
        current_trends_block = f"""

LIVE WEB SEARCH — CURRENT DEVELOPMENTS OUTSIDE THE ACADEMIC LITERATURE (real, live search results):
{trend_lines}"""

    prompt = f"""You are forecasting where a research area is headed, using ONLY the real signal below — do not invent statistics or named papers.

RESEARCH TOPIC: {topic}

REAL KEYWORD TREND SIGNAL (computed from actual word/keyword frequencies across the collected corpus):
- RISING keywords: {rising}
- ENTRENCHED keywords: {entrenched}
- DECLINING keywords: {declining}
- FASTEST-GROWING keywords by real year-over-year change: {fastest_growing}
- KEYWORD CLUSTERS (terms that real co-occurrence groups together): {clusters_str}
- KEYWORD CATEGORIES (dataset / model / method / application terms observed): {categories_str}
{current_trends_block}

Using PAST (declining/entrenched) -> PRESENT (rising/fastest-growing) -> FUTURE, forecast where this field is headed. Identify technology convergence (clusters or categories that look likely to combine but haven't yet) and under-explored combinations grounded in the signal above.

Provide pure JSON, no markdown:
{{
  "past_to_present_summary": "2-3 sentences on the real shift from declining/entrenched to rising terms",
  "emerging_research_areas": ["short phrase", "short phrase", "..."],
  "rapidly_growing_keywords": {list(dict.fromkeys(trend_stats.get('rising_keywords', [])[:10]))},
  "under_explored_combinations": ["a pair/cluster of real terms that rarely co-occur despite both being prominent, with a one-sentence reason", "..."],
  "technology_convergence": ["a real cluster or cross-category combination that looks likely to converge soon, with a one-sentence reason", "..."],
  "future_research_directions": ["concrete 1-3 year direction", "..."],
  "confidence_note": "One sentence on how strong/weak this forecast's grounding is (e.g. sparse recent data, thin current-trends evidence)"
}}"""
    response = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3, max_tokens=1500,
    )
    raw = response.choices[0].message.content.strip()
    raw = re.sub(r'^```[a-z]*\n?', '', raw)
    raw = re.sub(r'\n?```$', '', raw)
    raw = re.sub(r'\\(?!["\\bfnrtu/]|u[0-9a-fA-F]{4})', '', raw)
    return json.loads(raw)


def analyze_deep_research_gaps_with_groq(api_key, topic, papers, trend_stats, current_trends=None, forecast=None):
    """
    Gap detection for the deep-collection pipeline (deep_collection.py):
    the input here is the Stage-2 top-(up to 500) impact-ranked papers
    plus real word-frequency trend stats computed across them (which
    keywords are RISING in the most recent papers vs. ENTRENCHED across
    the whole set — see deep_collection.keyword_trend_stats). The LLM
    only interprets that real signal into named gaps + a near-term
    outlook; it never invents the trend data itself.

    `current_trends`: optional list of real, live web-search results
    (see current_trends.py) reflecting what's currently being discussed
    about this topic OUTSIDE the academic literature — new tools,
    models, datasets, industry requirements that may not have made it
    into a peer-reviewed paper yet. When present, the LLM is asked to
    check whether the collected corpus already reflects these
    developments or lags behind them (flowchart stage 15). When absent
    (no search-provider key configured), this section is omitted from
    the prompt entirely rather than asking the LLM to guess at "current
    trends" with no real external signal to ground it in.

    Every collected paper is sent to the LLM as evidence — split into
    token-budget-safe batches (see _chunk_papers_by_token_budget) so a
    1000-paper corpus doesn't get silently truncated to whatever fits
    in one Groq call. Each batch gets its own gap-detection call against
    the same trend_stats/current_trends/forecast context; the resulting
    gaps are merged and deduplicated across all batches.
    """
    client = Groq(api_key=api_key)
    line_fn = lambda p: (
        f"- {p['title']} ({p.get('year','N/A')}, {p.get('source','')}, "
        f"impact_score={p.get('impact_score', 0)}"
        f"{', citations=' + str(int(p['citation_count'])) if p.get('citation_count') is not None else ''})"
    )
    batches = list(_chunk_papers_by_token_budget(papers, line_fn, reserved_tokens=4200, overhead_tokens=1400))
    merged = {"research_gaps": []}
    got_headline_fields = False
    succeeded = 0
    for i, batch in enumerate(batches):
        result = _analyze_deep_gaps_batch_with_groq(
            client, topic, papers, batch, trend_stats, current_trends, forecast, line_fn
        )
        if result is None:
            continue
        succeeded += 1
        if not got_headline_fields:
            merged["summary"] = result.get("summary", "")
            merged["trend_analysis"] = result.get("trend_analysis", {})
            merged["overall_future_outlook"] = result.get("overall_future_outlook", "")
            merged["gap_density"] = result.get("gap_density", "medium")
            merged["overall_recommendation"] = result.get("overall_recommendation", "")
            got_headline_fields = True
        merged["research_gaps"].extend(result.get("research_gaps", []))
        if i < len(batches) - 1:
            time.sleep(2.5)  # stay under the 30 RPM free-tier request cap
    if succeeded == 0:
        raise RuntimeError(
            f"Deep-research gap analysis failed on all {len(batches)} batch(es) — every "
            f"Groq call either errored or returned bad JSON. Check the "
            f"[deep-gap-analysis-batch] log lines above for the underlying Groq error."
        )
    merged["research_gaps"] = _dedupe_gaps(merged["research_gaps"])
    return merged


def _analyze_deep_gaps_batch_with_groq(client, topic, all_papers, top_evidence, trend_stats, current_trends, forecast, line_fn):
    papers = all_papers  # used below for len(papers) in the prompt's corpus-size line
    paper_lines = "\n".join(line_fn(p) for p in top_evidence)
    rising = ", ".join(trend_stats.get('rising_keywords', [])[:20]) or "none detected"
    entrenched = ", ".join(trend_stats.get('entrenched_keywords', [])[:20]) or "none detected"
    declining = ", ".join(trend_stats.get('declining_keywords', [])[:15]) or "none detected"
    cutoff = trend_stats.get('cutoff_year')
    n_recent = trend_stats.get('recent_paper_count', 0)
    n_established = trend_stats.get('established_paper_count', 0)
    growth = trend_stats.get('growth_rate', {})
    fastest_growing = ", ".join(
        f"{term} ({rate:+.0%})" for term, rate in sorted(growth.items(), key=lambda kv: kv[1], reverse=True)[:10]
    ) or "not enough year-over-year data"
    impact_weighted = trend_stats.get('impact_weighted_frequency', [])[:10]
    impact_weighted_str = ", ".join(f"{d['term']} ({d['weighted_score']})" for d in impact_weighted) or "none"
    co_occ = trend_stats.get('co_occurrence', [])[:8]
    co_occ_str = "; ".join(f"{d['pair'][0]} + {d['pair'][1]} ({d['count']}x)" for d in co_occ) or "none notable"

    current_trends_block = ""
    if current_trends:
        trend_lines = "\n".join(
            f"- {r.get('title', '')}: {r.get('snippet', '')[:300]}" for r in current_trends[:6]
        )
        current_trends_block = f"""

LIVE WEB SEARCH — WHAT'S CURRENTLY BEING DISCUSSED ABOUT THIS TOPIC OUTSIDE THE ACADEMIC LITERATURE (real, live search results, not academic papers — may reflect very recent tools/models/datasets/industry needs that haven't reached peer review yet):
{trend_lines}

For each gap, note whether the collected literature above already reflects what's in this live-search evidence, or whether it lags behind it — a real lag between "what's live right now" and "what's been peer-reviewed" is itself a meaningful research-gap signal."""

    forecast_block = ""
    if forecast:
        directions = "; ".join(forecast.get('future_research_directions', [])[:6]) or "none"
        convergence = "; ".join(forecast.get('technology_convergence', [])[:4]) or "none"
        under_explored = "; ".join(forecast.get('under_explored_combinations', [])[:4]) or "none"
        forecast_block = f"""

STAGE-16 TREND FORECAST (produced separately, from the same real keyword signal — treat this as informed context, not as gaps to just restate):
- Forecast future directions: {directions}
- Forecast technology convergence: {convergence}
- Forecast under-explored combinations: {under_explored}"""

    prompt = f"""You are an expert academic research analyst reviewing a large, impact-ranked literature set collected across many academic sources.

RESEARCH TOPIC: {topic}

CORPUS: {len(papers)} papers survived a two-stage collection+ranking pipeline (per-source top 100, then a combined top {min(len(papers), 500)} re-ranked by impact score). Below is the highest-impact slice as evidence.

TOP-IMPACT PAPERS (evidence, highest impact first):
{paper_lines}

KEYWORD TREND SIGNAL (computed directly from word/keyword frequencies across the full {len(papers)}-paper corpus, not guessed):
- Papers treated as "recent" (published {cutoff} or later): {n_recent}
- Papers treated as "established" (published before {cutoff}): {n_established}
- RISING keywords (notably more frequent in recent papers than established ones): {rising}
- ENTRENCHED keywords (consistently frequent across both recent and established papers): {entrenched}
- DECLINING keywords (were frequent in established papers, no longer among the top recent terms): {declining}
- FASTEST-GROWING keywords by real year-over-year frequency change (term (growth%)): {fastest_growing}
- IMPACT-WEIGHTED keywords — terms concentrated in the highest-impact papers, not just the most common ones (term (weighted score)): {impact_weighted_str}
- Frequently CO-OCCURRING keyword pairs within the same paper (a real signal of which sub-areas are already being combined): {co_occ_str}
{current_trends_block}
{forecast_block}

IMPORTANT: Ground each gap in this real trend signal — explain it in terms of what the field is now moving toward (rising/fastest-growing/impact-weighted keywords) versus what it has already saturated (entrenched/declining keywords), and note any relevant co-occurrence pattern (e.g. two terms that are NOT yet co-occurring, despite both being prominent, is itself a gap signal). Give a near-term forward-looking prediction for each gap. Identify BETWEEN 3 AND 6 distinct, non-overlapping research gaps from THIS batch of evidence (results from other batches of the same corpus are merged separately, so keep this batch's response focused and complete rather than exhaustive).

Provide a detailed JSON response (no markdown, pure JSON):
{{
  "summary": "2-3 sentence overview of where this literature currently stands",
  "trend_analysis": {{
    "rising_keywords": {trend_stats.get('rising_keywords', [])[:20]},
    "entrenched_keywords": {trend_stats.get('entrenched_keywords', [])[:20]},
    "interpretation": "2-3 sentences on what the rising-vs-entrenched shift suggests about where the field is heading"
  }},
  "research_gaps": [
    {{
      "gap_title": "Specific, distinct gap title grounded in the trend shift",
      "severity": "critical/major/moderate",
      "description": "Detailed description",
      "trend_signal": "Which rising and/or entrenched keyword(s) this gap is grounded in, and why",
      "future_prediction": "A concrete, near-term (1-3 year) prediction of how this gap is likely to be addressed or evolve",
      "why_important": "Why filling this gap matters",
      "suggested_approach": "How to address this gap",
      "estimated_impact": "Expected contribution",
      "evidence_papers": ["actual paper title from the list above", "another actual paper title"],
      "confidence_score": 85,
      "lags_current_developments": "If live-search evidence was provided above: does the literature already reflect it, or lag behind it? Empty string if no live-search evidence was provided."
    }}
  ],
  "overall_future_outlook": "2-3 sentences predicting where this research area is headed over the next 1-3 years, grounded in the rising keywords above",
  "gap_density": "high/medium/low",
  "overall_recommendation": "One powerful sentence"
}}"""
    try:
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3, max_tokens=4200
        )
    except Exception as e:
        print(f"[deep-gap-analysis-batch] skipped a batch after Groq error: {e}")
        return None
    return _parse_groq_json(response.choices[0].message.content, "deep-gap-analysis-batch")


def generate_pdf_report(topic, analysis, papers, report_path):
    doc = SimpleDocTemplate(report_path, pagesize=A4,
                            rightMargin=0.75*inch, leftMargin=0.75*inch,
                            topMargin=0.75*inch, bottomMargin=0.75*inch)
    styles = getSampleStyleSheet()
    story  = []
    title_style = ParagraphStyle('T', parent=styles['Title'], fontSize=22, textColor=colors.HexColor('#1a1a2e'), spaceAfter=6, alignment=TA_CENTER)
    h1 = ParagraphStyle('H1', parent=styles['Heading1'], fontSize=14, textColor=colors.HexColor('#16213e'), spaceBefore=14, spaceAfter=6)
    h2 = ParagraphStyle('H2', parent=styles['Heading2'], fontSize=12, textColor=colors.HexColor('#0f3460'), spaceBefore=10, spaceAfter=4)
    body = ParagraphStyle('B', parent=styles['Normal'], fontSize=10, leading=14, spaceAfter=6, alignment=TA_JUSTIFY)
    meta = ParagraphStyle('M', parent=styles['Normal'], fontSize=9, textColor=colors.HexColor('#666666'), spaceAfter=4, alignment=TA_CENTER)

    story.append(Paragraph("Research Gap Analysis Report", title_style))
    story.append(Paragraph(f"Topic: {topic}", meta))
    story.append(Paragraph(f"Generated: {datetime.datetime.now().strftime('%B %d, %Y at %H:%M')}", meta))
    story.append(HRFlowable(width="100%", thickness=2, color=colors.HexColor('#0f3460')))
    story.append(Spacer(1, 12))
    story.append(Paragraph("Executive Summary", h1))
    story.append(Paragraph(analysis.get('summary',''), body))
    story.append(Spacer(1, 8))

    # Scores
    novelty = analysis.get('novelty_score','N/A')
    gap_density = analysis.get('gap_density','N/A')
    t = Table([['Novelty Score','Gap Density','Papers Analyzed'],[f"{novelty}/100",gap_density.upper(),str(len(papers))]], colWidths=[2*inch,2*inch,2*inch])
    t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#0f3460')),('TEXTCOLOR',(0,0),(-1,0),colors.white),('ALIGN',(0,0),(-1,-1),'CENTER'),('FONTSIZE',(0,0),(-1,0),11),('FONTSIZE',(0,1),(-1,1),14),('FONTNAME',(0,1),(-1,1),'Helvetica-Bold'),('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.HexColor('#e8f4fd')]),('GRID',(0,0),(-1,-1),0.5,colors.HexColor('#cccccc')),('TOPPADDING',(0,0),(-1,-1),8),('BOTTOMPADDING',(0,0),(-1,-1),8)]))
    story.append(t)
    story.append(Spacer(1,12))

    # Research Gaps with Evidence
    story.append(Paragraph("Identified Research Gaps & Evidence", h1))
    sev_colors = {'critical':'#ff4757','major':'#ffa502','moderate':'#2ed573'}
    for i, gap in enumerate(analysis.get('research_gaps',[]), 1):
        sev = gap.get('severity','moderate')
        sev_color = sev_colors.get(sev,'#999999')
        story.append(Paragraph(f"Gap #{i}: {gap.get('gap_title','')}", h2))
        confidence = gap.get('confidence_score', 'N/A')
        evidence_papers = ', '.join(gap.get('evidence_papers',[]))
        gap_data = [
            ['Severity', Paragraph(f"{sev.upper()} | Confidence: {confidence}%", body)],
            ['Description', Paragraph(gap.get('description',''), body)],
            ['Why Important', Paragraph(gap.get('why_important',''), body)],
            ['Suggested Approach', Paragraph(gap.get('suggested_approach',''), body)],
            ['Expected Impact', Paragraph(gap.get('estimated_impact',''), body)],
            ['Evidence Papers', Paragraph(evidence_papers, body)],
            ['Gap Proof', Paragraph(gap.get('evidence_explanation',''), body)],
        ]
        t3 = Table(gap_data, colWidths=[1.5*inch, 5.5*inch])
        t3.splitByRow = 1
        t3.setStyle(TableStyle([('FONTNAME',(0,0),(0,-1),'Helvetica-Bold'),('FONTSIZE',(0,0),(-1,-1),9),('ALIGN',(0,0),(0,-1),'RIGHT'),('VALIGN',(0,0),(-1,-1),'TOP'),('BACKGROUND',(0,0),(0,0),colors.HexColor(sev_color)),('TEXTCOLOR',(0,0),(0,0),colors.white),('ROWBACKGROUNDS',(0,0),(-1,-1),[colors.HexColor('#fff9f9'),colors.white]),('GRID',(0,0),(-1,-1),0.3,colors.HexColor('#eeeeee')),('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),5),('LEFTPADDING',(0,0),(-1,-1),8)]))
        story.append(t3)
        story.append(Spacer(1,8))

    # Next Steps
    story.append(Paragraph("Recommended Next Steps", h1))
    for i, step in enumerate(analysis.get('recommended_next_steps',[]), 1):
        story.append(Paragraph(f"{i}. {step}", body))
    story.append(Spacer(1,8))

    # Related Papers
    story.append(Paragraph("Related Papers Found", h1))
    for p in papers[:10]:
        authors = ', '.join(p.get('authors',[])[:2])
        story.append(Paragraph(f"<b>{p['title']}</b> ({p.get('year','')}) — {authors}", body))

    story.append(Spacer(1,16))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor('#cccccc')))
    story.append(Paragraph("Generated by ResearchLens AI • Autonomous Research Gap Detection System", meta))
    doc.build(story)

# ─── PDF report (multi paper) ────────────────────────────────────────
def generate_multi_pdf_report(topic, analysis, papers_data, report_path):
    doc = SimpleDocTemplate(report_path, pagesize=A4,
                            rightMargin=0.75*inch, leftMargin=0.75*inch,
                            topMargin=0.75*inch, bottomMargin=0.75*inch)
    styles = getSampleStyleSheet()
    story  = []
    title_style = ParagraphStyle('T', parent=styles['Title'], fontSize=22, textColor=colors.HexColor('#1a1a2e'), spaceAfter=6, alignment=TA_CENTER)
    h1 = ParagraphStyle('H1', parent=styles['Heading1'], fontSize=14, textColor=colors.HexColor('#16213e'), spaceBefore=14, spaceAfter=6)
    h2 = ParagraphStyle('H2', parent=styles['Heading2'], fontSize=12, textColor=colors.HexColor('#0f3460'), spaceBefore=10, spaceAfter=4)
    body = ParagraphStyle('B', parent=styles['Normal'], fontSize=10, leading=14, spaceAfter=6, alignment=TA_JUSTIFY)
    meta = ParagraphStyle('M', parent=styles['Normal'], fontSize=9, textColor=colors.HexColor('#666666'), spaceAfter=4, alignment=TA_CENTER)

    story.append(Paragraph("Multi-Paper Research Gap Analysis Report", title_style))
    story.append(Paragraph(f"Topic: {topic}", meta))
    story.append(Paragraph(f"Papers Analyzed: {len(papers_data)} | Generated: {datetime.datetime.now().strftime('%B %d, %Y at %H:%M')}", meta))
    story.append(HRFlowable(width="100%", thickness=2, color=colors.HexColor('#0f3460')))
    story.append(Spacer(1,12))

    # Literature Review
    lit = analysis.get('literature_review', {})
    story.append(Paragraph("Literature Review", h1))
    for section, content in [('Introduction', lit.get('introduction','')), ('Main Findings', lit.get('main_findings','')), ('Methodologies Used', lit.get('methodologies','')), ('Limitations', lit.get('limitations','')), ('Conclusion', lit.get('conclusion',''))]:
        story.append(Paragraph(section, h2))
        story.append(Paragraph(content, body))
    story.append(Spacer(1,12))

    # Individual paper summaries
    story.append(Paragraph("Individual Paper Summaries", h1))
    for ps in analysis.get('paper_summaries', []):
        story.append(Paragraph(ps.get('paper_name',''), h2))
        summ_data = [
            ['Objective', Paragraph(ps.get('objective',''), body)],
            ['Methodology', Paragraph(ps.get('methodology',''), body)],
            ['Dataset', Paragraph(ps.get('dataset','N/A'), body)],
            ['Key Results', Paragraph(ps.get('key_results',''), body)],
            ['Limitations', Paragraph(ps.get('limitations',''), body)],
        ]
        ts = Table(summ_data, colWidths=[1.5*inch, 5.5*inch])
        ts.setStyle(TableStyle([('FONTNAME',(0,0),(0,-1),'Helvetica-Bold'),('FONTSIZE',(0,0),(-1,-1),9),('VALIGN',(0,0),(-1,-1),'TOP'),('GRID',(0,0),(-1,-1),0.3,colors.HexColor('#eeeeee')),('ROWBACKGROUNDS',(0,0),(-1,-1),[colors.HexColor('#f5f9ff'),colors.white]),('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),5),('LEFTPADDING',(0,0),(-1,-1),8)]))
        story.append(ts)
        story.append(Spacer(1,8))

    # Research Gaps with Evidence
    story.append(Paragraph("Identified Research Gaps & Evidence", h1))
    sev_colors = {'critical':'#ff4757','major':'#ffa502','moderate':'#2ed573'}
    for i, gap in enumerate(analysis.get('research_gaps',[]), 1):
        sev = gap.get('severity','moderate')
        story.append(Paragraph(f"Gap #{i}: {gap.get('gap_title','')}", h2))
        confidence = gap.get('confidence_score','N/A')
        evidence_papers = ', '.join(gap.get('evidence_papers',[]))
        gap_data = [
            ['Severity', Paragraph(f"{sev.upper()} | Confidence: {confidence}%", body)],
            ['Description', Paragraph(gap.get('description',''), body)],
            ['Why Important', Paragraph(gap.get('why_important',''), body)],
            ['Suggested Approach', Paragraph(gap.get('suggested_approach',''), body)],
            ['Expected Impact', Paragraph(gap.get('estimated_impact',''), body)],
            ['Evidence Papers', Paragraph(evidence_papers, body)],
            ['Gap Proof', Paragraph(gap.get('evidence_explanation',''), body)],
        ]
        t3 = Table(gap_data, colWidths=[1.5*inch, 5.5*inch])
        t3.splitByRow = 1
        t3.setStyle(TableStyle([('FONTNAME',(0,0),(0,-1),'Helvetica-Bold'),('FONTSIZE',(0,0),(-1,-1),9),('ALIGN',(0,0),(0,-1),'RIGHT'),('VALIGN',(0,0),(-1,-1),'TOP'),('BACKGROUND',(0,0),(0,0),colors.HexColor(sev_colors.get(sev,'#999999'))),('TEXTCOLOR',(0,0),(0,0),colors.white),('ROWBACKGROUNDS',(0,0),(-1,-1),[colors.HexColor('#fff9f9'),colors.white]),('GRID',(0,0),(-1,-1),0.3,colors.HexColor('#eeeeee')),('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),5),('LEFTPADDING',(0,0),(-1,-1),8)]))
        story.append(t3)
        story.append(Spacer(1,8))

    story.append(Spacer(1,16))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor('#cccccc')))
    story.append(Paragraph("Generated by ResearchLens AI • Multi-Paper Research Gap Detection System", meta))
    doc.build(story)

# ─── Agentic AI layer ─────────────────────────────────────────────────
# Orchestrator Agent: registers every specialized agent below. The core
# agents are thin wrappers around the functions already defined above
# (dependency-injected, so this is a refactor of *structure*, not of
# *behavior* — /api/analyze and /api/analyze-multi now run as an
# explicit agent pipeline instead of one long function body, and every
# run produces an inspectable trace of what each agent did and why).
# Event bus: agent completions publish here; the default subscriber is
# just an audit-log print, but anything else (notifications, metrics)
# can subscribe without the orchestrator or any agent knowing about it.
event_bus = EventBus()
register_default_subscribers(event_bus)

# Shared memory/vector-store/cache infrastructure, backed by the same
# SQLite DB as everything else (see memory/ package for why this isn't
# Redis/FAISS at this app's scale).
context_memory = ContextMemory(get_db)
vector_store = VectorStore(get_db)
response_cache = TTLCache(default_ttl_seconds=300)

orchestrator = Orchestrator(event_bus=event_bus)
orchestrator.register(SearchAgent(fetch_papers_multi_source))
orchestrator.register(PDFAnalysisAgent(extract_pdf_text))
orchestrator.register(MultiPDFAnalysisAgent(extract_pdf_text, UPLOAD_FOLDER))
orchestrator.register(KeywordExtractionAgent(extract_keywords))
orchestrator.register(KnowledgeGraphAgent(build_knowledge_graph, graph_to_json, discover_graph_gaps))
orchestrator.register(GapDetectionAgent(analyze_gaps_with_groq, analyze_multiple_papers_with_groq))
orchestrator.register(TrendAnalysisAgent())
orchestrator.register(ReportGenerationAgent(generate_pdf_report, generate_multi_pdf_report))
orchestrator.register(MetadataAgent())
orchestrator.register(EmbeddingAgent(vector_store))
orchestrator.register(ValidationAgent())
orchestrator.register(PlannerAgent())
orchestrator.register(MemoryAgent(context_memory))
orchestrator.register(DomainClassificationAgent(classify_query, get_db))
orchestrator.register(DeepCollectionAgent(deep_collection.run_two_stage_collection, _build_deep_source_registry))
orchestrator.register(TrendGapDetectionAgent(deep_collection.keyword_trend_stats, analyze_deep_research_gaps_with_groq, deep_collection.validate_and_prioritize_gaps))
orchestrator.register(TrendForecastingAgent(deep_collection.keyword_trend_stats, forecast_research_trends_with_groq))
orchestrator.register(DomainUnderstandingAgent(analyze_domain_understanding_with_groq))
orchestrator.register(CurrentTrendsAgent(current_trends.web_search_current_trends, current_trends.current_trends_available))
orchestrator.register(FullTextAnalysisAgent(full_text_analysis.deep_analyze_top_papers, GROQ_MODEL))

# Publication Intelligence routes (/api/domains, /api/journals, /api/conferences,
# /api/societies, /api/publishers, /api/classify-research-topic). Registered as
# its own blueprint so it can't collide with any existing @app.route below.
register_publication_routes(app, get_db)
# citation_agent and monitoring_agent are registered further down, right
# after papers_to_bibtex() and run_topic_check() are defined.
# Enterprise-level agents
orchestrator.register(RAGResearchAssistantAgent())
orchestrator.register(NoveltyVerificationAgent())
orchestrator.register(ResearchProposalAgent())
orchestrator.register(ExperimentRecommendationAgent())
orchestrator.register(ResearchImpactAgent())
orchestrator.register(ExplainabilityAgent())


@app.route('/api/agents/list', methods=['GET'])
@require_auth
def list_agents():
    """Exposes the live agent registry — useful for a UI that wants to show
    the pipeline architecture, or for debugging which agents are wired up."""
    return jsonify({'agents': orchestrator.list_agents()})


# ─── Single paper analysis endpoint ─────────────────────────────────
@app.route('/api/analyze', methods=['POST'])
@require_auth
def analyze():
    api_key  = request.form.get('api_key','').strip()
    topic    = request.form.get('topic','').strip()
    idea_description  = request.form.get('idea_description','').strip()
    primary_domain    = request.form.get('primary_domain','').strip()
    secondary_domains = request.form.get('secondary_domains','').strip()
    preferred_venues  = request.form.get('preferred_venues','').strip()
    paper_sources_raw = request.form.get('paper_sources','').strip()
    paper_sources = [s.strip() for s in paper_sources_raw.split(',') if s.strip()] if paper_sources_raw else None
    venue_filter_raw = request.form.get('venue_filter','').strip()
    venue_filter = [v.strip() for v in venue_filter_raw.split(',') if v.strip()] if venue_filter_raw else None
    pdf_file = request.files.get('pdf')
    if not api_key: return jsonify({'error':'Groq API key is required'}),400
    if not topic:   return jsonify({'error':'Research topic is required'}),400

    # The optional "Describe Your Idea" text enriches the topic used for
    # search / gap-detection / report generation (context['topic']), but
    # the plain `topic` variable stays untouched — that's what gets saved
    # to analysis history, so history stays readable.
    enriched_topic = f"{topic}. {idea_description}" if idea_description else topic

    context = {
        'api_key': api_key, 'topic': enriched_topic, 'mode': 'single',
        'idea_description': idea_description or None,
        'primary_domain': primary_domain or None,
        'secondary_domains': secondary_domains or None,
        'preferred_venues': preferred_venues or None,
        'paper_sources': paper_sources,  # None = search all sources (unchanged default)
        'venue_filter': venue_filter,    # None = no venue restriction (applies to Semantic Scholar only)
    }

    if pdf_file and pdf_file.filename:
        fname = f"{uuid.uuid4()}_{pdf_file.filename}"
        fpath = os.path.join(UPLOAD_FOLDER, fname)
        pdf_file.save(fpath)
        context['pdf_filepath'] = fpath

    # Orchestrated pipeline: PDF -> Search -> Keywords -> Gap Detection ->
    # Knowledge Graph -> Trends -> Report. Each step's error is mapped to
    # the same HTTP status/message the old inline code returned.
    r = orchestrator.run_agent('pdf_agent', context)
    if not r.success:
        return jsonify({'error': f'PDF processing failed: {r.error}'}), 500

    r = orchestrator.run_agent('search_agent', context)
    if not r.success:
        if isinstance(r.exception, ArxivUnavailableError):
            return jsonify({'error': str(r.exception), 'error_type': 'arxiv_unreachable', 'source_label': getattr(r.exception, 'source_label', 'ArXiv')}), 503
        return jsonify({'error': f'Search failed: {r.error}'}), 500

    # keyword_agent and metadata_agent both only need context['papers'] /
    # pdf_plain — neither depends on the other's output — so run them
    # concurrently instead of forcing an order.
    orchestrator.run_parallel(['keyword_agent', 'metadata_agent'], context)

    # Additive: classify the topic into the domain/society/publisher/journal
    # taxonomy. Never allowed to affect the existing pipeline's success path —
    # if it fails for any reason, the agent's own error is captured in the
    # trace and context['domain_classification'] simply stays unset.
    orchestrator.run_agent('domain_classification_agent', context)

    r = orchestrator.run_agent('gap_detection_agent', context)
    if not r.success:
        if isinstance(r.exception, json.JSONDecodeError):
            return jsonify({'error': f'AI returned malformed JSON. Try again. {r.error}'}), 500
        return jsonify({'error': f'AI analysis failed: {r.error}'}), 500

    papers = context['papers']
    keywords = context['keywords']
    metadata = context.get('metadata', [])
    pdf_plain = context.get('pdf_plain', '')
    analysis = context['analysis']

    # Span-level evidence verification: only trust an evidence_quote if it
    # genuinely appears in the user's uploaded paper. If the model invented
    # or paraphrased it, strip it rather than surface a fake quote.
    for gap in analysis.get('research_gaps', []):
        quote = gap.get('evidence_quote', '')
        verified = bool(quote) and _verify_quote_in_text(quote, pdf_plain)
        gap['evidence_verified'] = verified
        if not verified:
            gap['evidence_quote'] = ''
            gap['evidence_page'] = None

    # Build the graph and derive trends AFTER analysis so gap/trend nodes
    # from the LLM's own findings are part of the graph, not just text in
    # the report. Neither agent depends on the other's output, so they
    # run concurrently via the orchestrator's thread pool.
    parallel_results = orchestrator.run_parallel(['knowledge_graph_agent', 'trend_agent'], context)
    graph_data = context['graph']
    trend_data = parallel_results['trend_agent'].data
    year_data = trend_data['year_data']
    cat_data = trend_data['cat_data']

    # Chunk and embed the uploaded paper so the RAG assistant's retrieval
    # can be reused/inspected via the vector store rather than recomputed
    # from scratch on every question.
    if pdf_plain:
        from agents.textsim import chunk_text
        chunks = chunk_text(pdf_plain, 900, 150)
        context['embedding_collection'] = f"pending_{uuid.uuid4().hex[:12]}"
        context['embedding_items'] = [(str(i), c) for i, c in enumerate(chunks)]
        orchestrator.run_agent('embedding_agent', context)

    validation_result = orchestrator.run_agent('validation_agent', context)

    report_id = str(uuid.uuid4())[:8]
    report_filename = f"report_{report_id}.pdf"
    context['report_path'] = os.path.join(REPORTS_FOLDER, report_filename)
    # Report title should stay the concise topic the user typed, not the
    # enriched (topic + description) string used to drive search/gap
    # detection above.
    context['topic'] = topic
    orchestrator.run_agent('report_agent', context)
    record_generated_file(report_filename, g.uid)

    # Save analysis to DB for history/tracking — document_text lets the
    # RAG Research Assistant Agent answer follow-up questions later
    # without needing the PDF re-uploaded.
    analysis_id = None
    try:
        conn = get_db()
        cur = conn.execute(
            'INSERT INTO analyses (topic, owner_uid, analysis_json, papers_json, gaps_count, created_at, document_text) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (topic, g.uid, json.dumps(analysis), json.dumps(papers[:20]), len(analysis.get('research_gaps',[])), datetime.datetime.now().isoformat(), pdf_plain[:12000])
        )
        analysis_id = cur.lastrowid
        conn.commit()
        conn.close()
        # Re-key the embedding collection from the temporary pending_ id to
        # the real analysis_id now that we have one, so the RAG assistant
        # (and any future vector_store.search call) can look it up by
        # analysis_id like everything else does.
        pending_collection = context.get('embedding_collection')
        if pending_collection and analysis_id:
            conn2 = get_db()
            conn2.execute('UPDATE embeddings SET collection=? WHERE collection=?',
                          (f"analysis_{analysis_id}", pending_collection))
            conn2.commit()
            conn2.close()
    except Exception as e:
        print(f"DB save error: {e}")

    return jsonify({
        'analysis': analysis, 'papers': papers, 'keywords': keywords[:25],
        'metadata': metadata, 'graph': graph_data, 'year_data': year_data, 'cat_data': cat_data,
        'report_id': report_id, 'report_filename': report_filename, 'mode': 'single',
        'analysis_id': analysis_id, 'validation': validation_result.data,
        'domain_classification': context.get('domain_classification'),
        'idea_description': idea_description or None,
        'primary_domain': primary_domain or None,
        'secondary_domains': secondary_domains or None,
        'preferred_venues': preferred_venues or None,
        'agent_trace': trace_to_dicts(context.get('_trace', [])),
    })

# ─── Deep research: exhaustive multi-source collection + impact ranking ──
@app.route('/api/deep-research', methods=['POST'])
@require_auth
def deep_research():
    """
    Runs the full deep-collection pipeline (deep_collection.py):
      0. (Optional, on by default) Expands the raw topic/domain/keywords
         into real related search queries via the LLM (flowchart stage
         1 — DomainUnderstandingAgent), widening Stage-0 recall beyond
         the literal topic string. Disable with 'expand_queries': false.
      1. Selects sources relevant to the domain (flowchart stage 2):
         general-purpose sources (arXiv, Semantic Scholar, OpenAlex,
         Crossref, DOAJ, Internet Archive, IEEE Xplore, Springer Nature,
         ScienceDirect, CORE) are always included; subject-specific
         databases (PubMed/Europe PMC, ERIC, INSPIRE-HEP, NASA ADS) are
         only included when the domain/sub-domain/keywords/topic/
         research-question actually match that source's documented
         scope — an ambiguous domain falls back to including all of
         them. An explicit 'paper_sources' list bypasses this filtering
         entirely. Selected/skipped sources are reported back in
         'sources_selected'/'sources_skipped_by_domain'. Then pages
         every selected source (plus a dedicated DBLP entry per checked
         strict-venue conference) to near-exhaustion, subject to a
         per-source safety cap (see deep_collection.PER_SOURCE_CAP's
         docstring for why an unbounded "every paper ever published"
         fetch isn't realistic in one request).
      2. Backfills real citation counts, via Semantic Scholar's batch
         lookup by DOI/arXiv ID, for every paper whose own source didn't
         report a citation count natively.
      3. Scores every paper with a real-signal impact score — citation
         velocity, plus (when available) OpenAlex's real citation-
         percentile band and real per-venue impact stats — ranks, and
         shortlists the top 100 from each source individually.
      4. Combines all per-source shortlists, deduplicates, re-scores
         (now also looking up real author-influence h-index via
         Semantic Scholar/OpenAlex, and real institution-influence via
         OpenAlex, for any paper whose source exposed a real author
         ID — see 'authors_with_influence_data'/
         'institutions_with_influence_data' below) and re-ranks on the
         combined pool, and keeps the top 500 overall.
      5. (Optional, off by default — needs TAVILY_API_KEY/SERPER_API_KEY)
         Runs a real live web search for what's currently happening in
         this topic outside the academic literature (flowchart stage 15
         — CurrentTrendsAgent). Enable with 'include_current_trends': true.
      6. Produces a standalone past->present->future forecast from that
         same real trend signal (flowchart stage 16 — TrendForecastingAgent),
         BEFORE gap detection rather than folded into the same call.
      7. Compares keyword usage in recent vs. established papers within
         the top 500 and asks the LLM to identify research gaps grounded
         in that trend shift (plus the stage-16 forecast and step-5
         live-search evidence, if any), each with a near-term future
         prediction, then validates and prioritizes each gap against
         real corpus evidence — including an explicit "does this look
         already addressed?" check.
      8. (Optional, off by default) Extracts methodology/dataset/
         limitations/future-work for papers — REAL full text (arXiv PDF /
         Europe PMC open-access / CORE) wherever it genuinely exists,
         labeled abstract-inference for every other paper, so this
         covers every analyzed paper rather than silently skipping most
         of them (flowchart stages 4 & 12 — FullTextAnalysisAgent).
         Enable with 'include_full_text_analysis': true; defaults to
         every collected paper, or pass 'full_text_top_n' to bound it to
         a cheaper/faster slice by impact rank.

    This can take a while (multiple sources, each paged multiple times)
    — expect this endpoint to run considerably longer than /api/analyze,
    and longer still with the optional stages enabled.
    """
    data = request.get_json(silent=True) or {}
    api_key = (data.get('api_key') or '').strip()
    topic = (data.get('topic') or '').strip()
    paper_sources = data.get('paper_sources')       # optional list; default = all sources
    venue_filter = data.get('venue_filter')          # optional list of AAAI/NeurIPS/ICML/ICLR
    if not api_key:
        return jsonify({'error': 'Groq API key is required'}), 400
    if not topic:
        return jsonify({'error': 'Research topic is required'}), 400

    expand_queries = data.get('expand_queries', True)
    include_current_trends = bool(data.get('include_current_trends', False))
    include_full_text_analysis = bool(data.get('include_full_text_analysis', False))
    # None = analyze every collected paper (flowchart stage 12's "every
    # selected paper"); caller may pass an int to bound it.
    _raw_top_n = data.get('full_text_top_n')
    full_text_top_n = int(_raw_top_n) if _raw_top_n not in (None, '') else None

    context = {
        'api_key': api_key, 'topic': topic,
        'paper_sources': paper_sources, 'venue_filter': venue_filter,
        'domain': data.get('domain'), 'sub_domain': data.get('sub_domain'),
        'keywords': data.get('keywords'), 'research_question': data.get('research_question'),
        'full_text_top_n': full_text_top_n,
    }

    if expand_queries:
        r = orchestrator.run_agent('domain_understanding_agent', context)
        if not r.success:
            # Query expansion failing shouldn't block the whole pipeline —
            # fall back to just the literal topic, same as if the caller
            # had disabled it.
            print(f"deep_research: domain understanding failed, continuing without it: {r.error}")
            context.setdefault('deep_extra_queries', [])

    r = orchestrator.run_agent('deep_collection_agent', context)
    if not r.success:
        return jsonify({'error': f'Deep collection failed: {r.error}'}), 500
    if not context.get('deep_papers'):
        return jsonify({'error': 'No papers were collected from any selected source for this topic.'}), 503

    if include_current_trends:
        r = orchestrator.run_agent('current_trends_agent', context)
        if not r.success:
            print(f"deep_research: current-trends search failed, continuing without it: {r.error}")
            context.setdefault('deep_current_trends', [])

    r = orchestrator.run_agent('trend_forecasting_agent', context)
    if not r.success:
        # Forecasting failing shouldn't block gap detection — it just
        # runs without a stage-16 forecast to build on, same as if this
        # step were skipped outright.
        print(f"deep_research: trend forecasting failed, continuing without it: {r.error}")
        context.setdefault('deep_trend_forecast', None)

    r = orchestrator.run_agent('trend_gap_detection_agent', context)
    if not r.success:
        return jsonify({'error': f'Trend/gap analysis failed: {r.error}'}), 500

    if include_full_text_analysis:
        r = orchestrator.run_agent('full_text_analysis_agent', context)
        if not r.success:
            print(f"deep_research: full-text analysis failed, continuing without it: {r.error}")

    # Keep the response payload reasonable — full paper metadata for the
    # top 100 (what most UIs will actually list), lighter title/score-only
    # entries for the rest of the top 500.
    top_papers_full = context['deep_papers'][:100]
    top_papers_rest = [
        {'title': p.get('title'), 'year': p.get('year'), 'source': p.get('source'),
         'impact_score': p.get('impact_score'), 'impact_score_basis': p.get('impact_score_basis'),
         'combined_rank': p.get('combined_rank'), 'deep_analysis': p.get('deep_analysis')}
        for p in context['deep_papers'][100:]
    ]

    return jsonify({
        'topic': topic,
        'domain_understanding': context.get('deep_domain_understanding'),
        'queries_used': context.get('deep_queries_used', [topic]),
        'stage0_stats': context.get('deep_stage0_stats'),
        'stage1_stats': context.get('deep_stage1_stats'),
        'stage2_count': len(context['deep_papers']),
        'citations_backfilled': context.get('deep_citations_backfilled'),
        'sources_with_citation_data': context.get('deep_sources_with_citation_data'),
        'sources_recency_only': context.get('deep_sources_recency_only'),
        'sources_selected': context.get('deep_sources_selected'),
        'sources_skipped_by_domain': context.get('deep_sources_skipped_by_domain'),
        'authors_with_influence_data': context.get('deep_authors_with_influence_data'),
        'institutions_with_influence_data': context.get('deep_institutions_with_influence_data'),
        'top_papers': top_papers_full,
        'remaining_papers': top_papers_rest,
        'trend_stats': context.get('deep_trend_stats'),
        'trend_forecast': context.get('deep_trend_forecast'),
        'current_trends_available': context.get('deep_current_trends_available', False),
        'current_trends': context.get('deep_current_trends', []),
        'full_text_analyzed_count': context.get('deep_full_text_analyzed_count'),
        'abstract_inference_count': context.get('deep_abstract_inference_count'),
        'analysis': context.get('deep_analysis'),
        'agent_trace': trace_to_dicts(context.get('_trace', [])),
    })

# ─── Multi-paper analysis endpoint ───────────────────────────────────
@app.route('/api/analyze-multi', methods=['POST'])
@require_auth
def analyze_multi():
    api_key = request.form.get('api_key','').strip()
    topic   = request.form.get('topic','').strip()
    idea_description  = request.form.get('idea_description','').strip()
    primary_domain    = request.form.get('primary_domain','').strip()
    secondary_domains = request.form.get('secondary_domains','').strip()
    preferred_venues  = request.form.get('preferred_venues','').strip()
    paper_sources_raw = request.form.get('paper_sources','').strip()
    paper_sources = [s.strip() for s in paper_sources_raw.split(',') if s.strip()] if paper_sources_raw else None
    venue_filter_raw = request.form.get('venue_filter','').strip()
    venue_filter = [v.strip() for v in venue_filter_raw.split(',') if v.strip()] if venue_filter_raw else None
    if not api_key: return jsonify({'error':'Groq API key is required'}),400
    if not topic:   return jsonify({'error':'Research topic is required'}),400

    pdf_files = request.files.getlist('pdfs')
    if not pdf_files or len(pdf_files)==0:
        return jsonify({'error':'Please upload at least one PDF'}),400
    if len(pdf_files)>5:
        return jsonify({'error':'Maximum 5 papers allowed'}),400

    enriched_topic = f"{topic}. {idea_description}" if idea_description else topic
    context = {
        'api_key': api_key, 'topic': enriched_topic, 'mode': 'multi', 'pdf_files': pdf_files,
        'idea_description': idea_description or None,
        'primary_domain': primary_domain or None,
        'secondary_domains': secondary_domains or None,
        'preferred_venues': preferred_venues or None,
        'paper_sources': paper_sources,
        'venue_filter': venue_filter,
    }

    r = orchestrator.run_agent('multi_pdf_agent', context)
    if not r.success:
        return jsonify({'error': f'PDF processing failed: {r.error}'}), 500
    papers_data = context['papers_data']
    if not papers_data:
        return jsonify({'error':'No valid PDFs found'}),400

    context['max_results'] = 40
    r = orchestrator.run_agent('search_agent', context)
    if not r.success:
        if isinstance(r.exception, ArxivUnavailableError):
            return jsonify({'error': str(r.exception), 'error_type': 'arxiv_unreachable', 'source_label': getattr(r.exception, 'source_label', 'ArXiv')}), 503
        return jsonify({'error': f'Search failed: {r.error}'}), 500
    arxiv_papers = context['papers']

    orchestrator.run_parallel(['keyword_agent', 'metadata_agent'], context)
    keywords = context['keywords']
    metadata = context.get('metadata', [])

    orchestrator.run_agent('domain_classification_agent', context)

    r = orchestrator.run_agent('gap_detection_agent', context)
    if not r.success:
        if isinstance(r.exception, json.JSONDecodeError):
            return jsonify({'error': f'AI returned malformed JSON. Try again. {r.error}'}), 500
        return jsonify({'error': f'AI analysis failed: {r.error}'}), 500
    analysis = context['analysis']

    parallel_results = orchestrator.run_parallel(['knowledge_graph_agent', 'trend_agent'], context)
    graph_data = context['graph']
    trend_data = parallel_results['trend_agent'].data
    year_data = trend_data['year_data']
    cat_data = trend_data['cat_data']

    validation_result = orchestrator.run_agent('validation_agent', context)

    report_id = str(uuid.uuid4())[:8]
    report_filename = f"multi_report_{report_id}.pdf"
    context['report_path'] = os.path.join(REPORTS_FOLDER, report_filename)
    context['topic'] = topic
    orchestrator.run_agent('report_agent', context)
    record_generated_file(report_filename, g.uid)

    # Persist for RAG follow-up questions across all uploaded papers
    analysis_id = None
    try:
        conn = get_db()
        combined_plain = "\n\n".join(f"[{pd['name']}]\n{pd['plain']}" for pd in papers_data)
        cur = conn.execute(
            'INSERT INTO analyses (topic, owner_uid, analysis_json, papers_json, gaps_count, created_at, document_text) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (topic, g.uid, json.dumps(analysis), json.dumps(arxiv_papers[:20]), len(analysis.get('research_gaps',[])), datetime.datetime.now().isoformat(), combined_plain[:12000])
        )
        analysis_id = cur.lastrowid
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"DB save error: {e}")

    return jsonify({
        'analysis': analysis, 'papers': arxiv_papers, 'uploaded_papers': papers_data,
        'keywords': keywords[:25], 'metadata': metadata, 'graph': graph_data, 'year_data': year_data,
        'cat_data': cat_data, 'report_id': report_id, 'report_filename': report_filename,
        'mode': 'multi', 'analysis_id': analysis_id, 'validation': validation_result.data,
        'domain_classification': context.get('domain_classification'),
        'idea_description': idea_description or None,
        'primary_domain': primary_domain or None,
        'secondary_domains': secondary_domains or None,
        'preferred_venues': preferred_venues or None,
        'agent_trace': trace_to_dicts(context.get('_trace', [])),
    })

@app.route('/api/download/<filename>')
@require_auth
def download_report(filename):
    # 1) Path traversal: strip any directory components (`../../etc/passwd`,
    #    absolute paths, etc.) before it ever touches the filesystem.
    safe_name = secure_filename(filename)
    if not safe_name or safe_name != filename:
        return jsonify({'error': 'Invalid filename'}), 400

    full_path = os.path.realpath(os.path.join(REPORTS_FOLDER, safe_name))
    reports_root = os.path.realpath(REPORTS_FOLDER)
    if os.path.commonpath([full_path, reports_root]) != reports_root:
        return jsonify({'error': 'Invalid filename'}), 400
    if not os.path.isfile(full_path):
        return jsonify({'error': 'Not found'}), 404

    # 2) IDOR: only the user who generated this file (or, for legacy rows
    #    created before ownership tracking existed, nobody) may download it.
    conn = get_db()
    row = conn.execute('SELECT owner_uid FROM generated_files WHERE filename=?', (safe_name,)).fetchone()
    conn.close()
    if not row or row['owner_uid'] != g.uid:
        return jsonify({'error': 'Not found'}), 404

    return send_file(full_path, as_attachment=True, download_name=safe_name)

# ─── BibTeX export ────────────────────────────────────────────────────
def _bibtex_base_key(paper):
    first_author = 'unknown'
    authors = paper.get('authors') or []
    if authors and authors[0]:
        parts = str(authors[0]).split()
        if parts:
            first_author = re.sub(r'[^a-zA-Z]', '', parts[-1]) or 'unknown'
    year = re.sub(r'[^0-9]', '', str(paper.get('year', '')))[:4] or 'nd'
    return f"{first_author}{year}".lower()

def papers_to_bibtex(papers, topic=''):
    # Sort by first author surname, then year — reads far better than raw
    # fetch order, and matches how a human would organize a reference list
    def sort_key(p):
        base = _bibtex_base_key(p)
        return (base[:-4] if base[-4:].isdigit() else base, p.get('year', ''))
    sorted_papers = sorted(papers, key=sort_key)

    # Disambiguate keys only when two entries genuinely collide (author2024,
    # author2024b, ...) instead of forcing a numeric suffix on every entry
    used_keys = {}
    entries = []
    for p in sorted_papers:
        base_key = _bibtex_base_key(p)
        if base_key in used_keys:
            used_keys[base_key] += 1
            suffix = chr(ord('a') + used_keys[base_key] - 1)
            key = f"{base_key}{suffix}"
        else:
            used_keys[base_key] = 0
            key = base_key

        title = (p.get('title') or 'Untitled').replace('{', '').replace('}', '')
        author_list = [a for a in (p.get('authors') or []) if a]
        authors = ' and '.join(author_list) or 'Unknown'
        year = re.sub(r'[^0-9]', '', str(p.get('year', ''))) or 'n.d.'
        source = p.get('source', 'Preprint')
        entry_type = 'article' if source in ('Semantic Scholar', 'OpenAlex') else 'misc'
        search_url = f"https://arxiv.org/search/?searchtype=all&query={requests.utils.quote(p.get('title') or '')}"

        lines = [f"@{entry_type}{{{key},"]
        lines.append(f'  title     = {{{title}}},')
        lines.append(f'  author    = {{{authors}}},')
        lines.append(f'  year      = {{{year}}},')
        lines.append(f'  url       = {{{search_url}}},')
        lines.append(f'  note      = {{Source: {source}}},')
        lines.append('}')
        entries.append('\n'.join(lines))

    header = (
        f"% {'='*60}\n"
        f"%  References exported from ResearchLens AI\n"
        + (f"%  Topic: {topic}\n" if topic else "")
        + f"%  Generated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"%  Total entries: {len(entries)}\n"
        f"% {'='*60}\n\n"
    )
    return header + '\n\n'.join(entries) + '\n'

orchestrator.register(CitationAgent(papers_to_bibtex))

@app.route('/api/export/bibtex', methods=['POST'])
@require_auth
def export_bibtex():
    """
    Accepts the paper list already returned by /api/analyze or
    /api/analyze-multi (frontend just re-posts it) and returns a .bib file,
    ready to import into Zotero/Mendeley/EndNote.
    """
    try:
        data = request.get_json(silent=True) or {}
        papers = data.get('papers', [])
        topic = data.get('topic', '')
        if not papers:
            return jsonify({'error': 'No papers provided to export'}), 400
        bibtex_content = papers_to_bibtex(papers, topic)
        bib_id = str(uuid.uuid4())[:8]
        bib_filename = f"references_{bib_id}.bib"
        bib_path = os.path.join(REPORTS_FOLDER, bib_filename)
        with open(bib_path, 'w', encoding='utf-8') as f:
            f.write(bibtex_content)
        return send_file(bib_path, as_attachment=True, download_name='references.bib', mimetype='application/x-bibtex')
    except Exception as e:
        print(f"[BibTeX export] Failed: {e}")
        return jsonify({'error': f'Could not generate BibTeX file: {str(e)}'}), 500

# ─── Enterprise-level agents ────────────────────────────────────────
# All six read a previously-saved analysis by `analysis_id` (returned
# from /api/analyze and /api/analyze-multi) so the user doesn't have to
# re-upload PDFs or re-run search for every follow-up action.

def _get_owned_analysis(analysis_id, uid):
    """Loads an analyses row the caller owns, or None. Returns a dict with
    topic/analysis/papers/document_text already JSON-decoded."""
    conn = get_db()
    row = conn.execute('SELECT * FROM analyses WHERE id=?', (analysis_id,)).fetchone()
    conn.close()
    if not row or row['owner_uid'] != uid:
        return None
    return {
        'topic': row['topic'],
        'analysis': json.loads(row['analysis_json']),
        'papers': json.loads(row['papers_json']) if row['papers_json'] else [],
        'document_text': row['document_text'] or '',
    }


@app.route('/api/agents/ask', methods=['POST'])
@require_auth
def agent_ask():
    """Research Assistant Agent — RAG Q&A over a previously analyzed paper."""
    data = request.get_json(silent=True) or {}
    api_key = data.get('api_key', '').strip()
    question = data.get('question', '').strip()
    analysis_id = data.get('analysis_id')
    if not api_key or not question or not analysis_id:
        return jsonify({'error': 'api_key, question, and analysis_id are required'}), 400
    saved = _get_owned_analysis(analysis_id, g.uid)
    if not saved:
        return jsonify({'error': 'Analysis not found'}), 404
    context = {'api_key': api_key, 'question': question, 'document_text': saved['document_text']}
    r = orchestrator.run_agent('rag_assistant_agent', context)
    if not r.success:
        return jsonify({'error': f'Could not answer question: {r.error}'}), 500
    # Persist this turn to conversation memory so a future "continue from
    # what we just discussed" question has somewhere to look — see
    # ContextMemory.append_conversation. Best-effort: a memory write
    # failure shouldn't fail an otherwise-successful answer.
    try:
        thread_key = f"analysis_{analysis_id}"
        context_memory.append_conversation(g.uid, thread_key, 'user', question, analysis_id)
        context_memory.append_conversation(g.uid, thread_key, 'assistant', r.data.get('answer', ''), analysis_id)
    except Exception as e:
        print(f"Memory write failed: {e}")
    return jsonify({**r.data, 'agent_explanation': r.explanation})


@app.route('/api/agents/memory/<int:analysis_id>', methods=['GET'])
@require_auth
def get_conversation_memory(analysis_id):
    """Returns the stored Q&A conversation history for a given analysis —
    what the Memory Agent has retained so a follow-up question can build
    on it instead of starting cold."""
    saved = _get_owned_analysis(analysis_id, g.uid)
    if not saved:
        return jsonify({'error': 'Analysis not found'}), 404
    history = context_memory.get(g.uid, 'conversation', f"analysis_{analysis_id}", analysis_id, default=[])
    return jsonify({'analysis_id': analysis_id, 'conversation': history})


@app.route('/api/agents/plan', methods=['POST'])
@require_auth
def agent_plan():
    """Planner Agent — looks up which agents a named task actually
    requires. See PlannerAgent / _TASK_PLANS in enterprise_agents.py."""
    data = request.get_json(silent=True) or {}
    task = data.get('task', '')
    context = {'task': task}
    r = orchestrator.run_agent('planner_agent', context)
    return jsonify({**r.data, 'agent_explanation': r.explanation})


@app.route('/api/agents/novelty', methods=['POST'])
@require_auth
def agent_novelty():
    """Novelty Verification Agent — how similar is a proposed idea to fetched literature."""
    data = request.get_json(silent=True) or {}
    api_key = data.get('api_key', '').strip()
    idea = data.get('idea', '').strip()
    analysis_id = data.get('analysis_id')
    if not idea or not analysis_id:
        return jsonify({'error': 'idea and analysis_id are required'}), 400
    saved = _get_owned_analysis(analysis_id, g.uid)
    if not saved:
        return jsonify({'error': 'Analysis not found'}), 404
    context = {'api_key': api_key, 'idea': idea, 'papers': saved['papers']}
    r = orchestrator.run_agent('novelty_agent', context)
    if not r.success:
        return jsonify({'error': f'Novelty check failed: {r.error}'}), 500
    return jsonify({**r.data, 'agent_explanation': r.explanation})


@app.route('/api/agents/proposal', methods=['POST'])
@require_auth
def agent_proposal():
    """Research Proposal Agent — generates a structured proposal from a chosen gap."""
    data = request.get_json(silent=True) or {}
    api_key = data.get('api_key', '').strip()
    analysis_id = data.get('analysis_id')
    gap_index = data.get('gap_index')
    if not api_key or not analysis_id:
        return jsonify({'error': 'api_key and analysis_id are required'}), 400
    saved = _get_owned_analysis(analysis_id, g.uid)
    if not saved:
        return jsonify({'error': 'Analysis not found'}), 404
    gaps = saved['analysis'].get('research_gaps', [])
    gap = gaps[gap_index] if isinstance(gap_index, int) and 0 <= gap_index < len(gaps) else {}
    context = {'api_key': api_key, 'topic': saved['topic'], 'gap': gap}
    r = orchestrator.run_agent('proposal_agent', context)
    if not r.success:
        return jsonify({'error': f'Proposal generation failed: {r.error}'}), 500
    return jsonify({'proposal': r.data, 'agent_explanation': r.explanation})


@app.route('/api/agents/experiment', methods=['POST'])
@require_auth
def agent_experiment():
    """Experiment Recommendation Agent — datasets, metrics, and baselines for a gap."""
    data = request.get_json(silent=True) or {}
    api_key = data.get('api_key', '').strip()
    analysis_id = data.get('analysis_id')
    gap_index = data.get('gap_index')
    if not api_key or not analysis_id:
        return jsonify({'error': 'api_key and analysis_id are required'}), 400
    saved = _get_owned_analysis(analysis_id, g.uid)
    if not saved:
        return jsonify({'error': 'Analysis not found'}), 404
    gaps = saved['analysis'].get('research_gaps', [])
    gap = gaps[gap_index] if isinstance(gap_index, int) and 0 <= gap_index < len(gaps) else {}
    context = {'api_key': api_key, 'topic': saved['topic'], 'gap': gap}
    r = orchestrator.run_agent('experiment_agent', context)
    if not r.success:
        return jsonify({'error': f'Experiment recommendation failed: {r.error}'}), 500
    return jsonify({'experiment_plan': r.data, 'agent_explanation': r.explanation})


@app.route('/api/agents/impact', methods=['POST'])
@require_auth
def agent_impact():
    """Research Impact Agent — estimates impact/momentum from publication trends."""
    data = request.get_json(silent=True) or {}
    api_key = data.get('api_key', '').strip()
    analysis_id = data.get('analysis_id')
    if not analysis_id:
        return jsonify({'error': 'analysis_id is required'}), 400
    saved = _get_owned_analysis(analysis_id, g.uid)
    if not saved:
        return jsonify({'error': 'Analysis not found'}), 404
    context = {'api_key': api_key, 'topic': saved['topic'], 'papers': saved['papers']}
    r = orchestrator.run_agent('impact_agent', context)
    if not r.success:
        return jsonify({'error': f'Impact estimation failed: {r.error}'}), 500
    return jsonify({**r.data, 'agent_explanation': r.explanation})


@app.route('/api/agents/explain', methods=['POST'])
@require_auth
def agent_explain():
    """Explainability Agent — plain-language explanation of why a gap was surfaced."""
    data = request.get_json(silent=True) or {}
    api_key = data.get('api_key', '').strip()
    analysis_id = data.get('analysis_id')
    gap_index = data.get('gap_index')
    if not analysis_id:
        return jsonify({'error': 'analysis_id is required'}), 400
    saved = _get_owned_analysis(analysis_id, g.uid)
    if not saved:
        return jsonify({'error': 'Analysis not found'}), 404
    gaps = saved['analysis'].get('research_gaps', [])
    gap = gaps[gap_index] if isinstance(gap_index, int) and 0 <= gap_index < len(gaps) else {}

    # Reconstruct a lightweight trace of what upstream agents contributed,
    # since the original pipeline's live trace isn't persisted across
    # requests — this is derived cheaply (no LLM calls) from saved data.
    papers = saved['papers']
    sources = sorted(set(p.get('source', '') for p in papers))
    keywords = extract_keywords(" ".join(p.get('title', '') + ' ' + p.get('abstract', '') for p in papers), top_n=30)
    pseudo_trace = [
        {'agent': 'search_agent', 'summary': f"Queried {', '.join(sources) or 'no'} sources for \"{saved['topic']}\" and retrieved {len(papers)} papers.", 'success': True},
        {'agent': 'keyword_agent', 'summary': f"Derived {len(keywords)} key concepts from the retrieved papers.", 'success': True},
        {'agent': 'gap_detection_agent', 'summary': f"LLM identified this gap with evidence from: {', '.join(gap.get('evidence_papers', [])[:3]) or 'the fetched literature'}.", 'success': True},
    ]
    context = {'api_key': api_key, 'target': gap, '_trace': pseudo_trace}
    r = orchestrator.run_agent('explainability_agent', context)
    if not r.success:
        return jsonify({'error': f'Explanation failed: {r.error}'}), 500
    return jsonify({**r.data, 'agent_explanation': r.explanation})


@app.route('/api/graph-qa', methods=['POST'])
@require_auth
def graph_qa():
    data = request.get_json()
    api_key  = data.get('api_key','').strip()
    question = data.get('question','').strip()
    if not api_key or not question:
        return jsonify({'error':'Missing api_key or question'}), 400
    try:
        client = Groq(api_key=api_key)
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role":"user","content":question}],
            temperature=0.3, max_tokens=400
        )
        return jsonify({'answer': response.choices[0].message.content.strip()})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/gap-litreview', methods=['POST'])
@require_auth
def gap_litreview():
    data = request.get_json()
    api_key = data.get('api_key','').strip()
    if not api_key:
        return jsonify({'error':'Missing api_key'}), 400
    try:
        client = Groq(api_key=api_key)
        gap_title = data.get('gap_title','')
        gap_desc  = data.get('gap_description','')
        approach  = data.get('suggested_approach','')
        evidence  = ', '.join(data.get('evidence_papers',[]))
        topic     = data.get('topic','')
        papers    = data.get('related_papers','')
        prompt = (
            "Generate a mini literature review and comparison table for this research gap.\n\n"
            f"TOPIC: {topic}\nGAP: {gap_title}\nDESCRIPTION: {gap_desc}\n"
            f"APPROACH: {approach}\nEVIDENCE PAPERS: {evidence}\nRELATED PAPERS:\n{papers}\n\n"
            "Return pure JSON only:\n"
            '{"literature_review":{"introduction":"...","existing_work":"...","gap_analysis":"...","proposed_direction":"..."},'
            '"comparison_table":[{"paper":"name","method":"method","covers_gap":false,"limitation":"what it misses"}]}'
        )
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role":"user","content":prompt}],
            temperature=0.3, max_tokens=1500
        )
        raw = response.choices[0].message.content.strip()
        raw = re.sub(r'^```[a-z]*\n?','',raw)
        raw = re.sub(r'\n?```$','',raw)
        raw = re.sub(r'\\(?!["\\bfnrtu/]|u[0-9a-fA-F]{4})', '', raw)
        return jsonify(json.loads(raw))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ═══════════════════════════════════════════════════
# WORKFLOW FEATURES: Funding Fit, Related Work Draft, Advisor Brief
# ═══════════════════════════════════════════════════

@app.route('/api/gap-funding', methods=['POST'])
@require_auth
def gap_funding():
    """
    Suggests which TYPES of funding bodies/programs fit a gap's domain,
    with real stable portal links to search live open calls yourself.

    Deliberately does NOT invent specific grant numbers, amounts, or
    deadlines — a live, verified funding-call database is a real product
    (see the earlier system-design conversation on why that's a multi-month
    data-ingestion effort, not a prompt). Fabricating "NSF Award #48213,
    deadline March 2026" here would be the exact kind of confidently-wrong
    output this project has been careful to avoid everywhere else, and it's
    strictly worse than pointing you to the live portal since it can go
    stale or simply be wrong on a decision that costs real time and money.
    """
    data = request.get_json()
    api_key = data.get('api_key','').strip()
    if not api_key:
        return jsonify({'error':'Missing api_key'}), 400
    try:
        client = Groq(api_key=api_key)
        gap_title = data.get('gap_title','')
        gap_desc  = data.get('gap_description','')
        topic     = data.get('topic','')
        region    = data.get('region','Global')  # e.g. 'India', 'US', 'EU', 'Global'
        prompt = (
            "You are a research funding advisor. Given a research gap, identify the "
            "TYPES of funding bodies/programs (not specific grant IDs — those go stale "
            "and you don't have live data) that would plausibly fund work addressing it.\n\n"
            f"TOPIC: {topic}\nGAP: {gap_title}\nDESCRIPTION: {gap_desc}\nRESEARCHER REGION: {region}\n\n"
            "Return pure JSON only, matching this schema exactly:\n"
            '{"funding_fit":[{"body":"e.g. NSF (National Science Foundation)","program_area":'
            '"e.g. Computer and Information Science and Engineering (CISE)","why_relevant":'
            '"1-2 sentences on why this gap fits this body\'s scope","search_url":'
            '"the body\'s real, stable funding-search homepage URL"}],'
            '"search_keywords":["keyword phrases the researcher should search on each portal"],'
            '"disclaimer":"Program areas and relevance are AI-suggested starting points, not guarantees of eligibility or open calls — always verify current deadlines and criteria directly on each portal."}\n\n'
            "Prioritize real, well-known bodies relevant to the region: for India include DST/SERB, "
            "ICMR (if health-related), MeitY (if tech/AI-related); for US include NSF, NIH (if health), "
            "DARPA (if applicable); for EU include Horizon Europe/ERC; always include at least one "
            "globally-relevant option regardless of region. Use only real organization names and real "
            "top-level portal URLs (e.g. https://www.nsf.gov/funding, https://www.serbonline.in, "
            "https://reporter.nih.gov, https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/home) "
            "— never invent a URL you're not confident is real."
        )
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role":"user","content":prompt}],
            temperature=0.2, max_tokens=1200
        )
        raw = response.choices[0].message.content.strip()
        raw = re.sub(r'^```[a-z]*\n?','',raw)
        raw = re.sub(r'\n?```$','',raw)
        raw = re.sub(r'\\(?!["\\bfnrtu/]|u[0-9a-fA-F]{4})', '', raw)
        return jsonify(json.loads(raw))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/related-work', methods=['POST'])
@require_auth
def related_work():
    """
    Drafts a properly-cited 'Related Work' section covering ALL fetched
    papers for the analysis (not just one gap) — meant to be pasted
    straight into the user's own paper. Citation keys match the BibTeX
    export keys 1:1 so the .bib file and this section are usable together
    without renumbering anything.
    """
    data = request.get_json()
    api_key = data.get('api_key','').strip()
    if not api_key:
        return jsonify({'error':'Missing api_key'}), 400
    papers = data.get('papers', [])
    topic  = data.get('topic', '')
    if not papers:
        return jsonify({'error':'No papers provided'}), 400
    try:
        # Reuse the exact same key logic as the BibTeX export so citations
        # in this section match the .bib file the user already downloaded
        def sort_key(p):
            base = _bibtex_base_key(p)
            return (base[:-4] if base[-4:].isdigit() else base, p.get('year', ''))
        sorted_papers = sorted(papers[:15], key=sort_key)
        used_keys = {}
        keyed_papers = []
        for p in sorted_papers:
            base_key = _bibtex_base_key(p)
            if base_key in used_keys:
                used_keys[base_key] += 1
                key = f"{base_key}{chr(ord('a') + used_keys[base_key] - 1)}"
            else:
                used_keys[base_key] = 0
                key = base_key
            keyed_papers.append({**p, '_key': key})

        papers_block = "\n".join(
            f"[{p['_key']}] {p['title']} ({p.get('year','N/A')}) — {', '.join(p.get('authors',[])[:3])}: {p.get('abstract','')[:300]}"
            for p in keyed_papers
        )
        client = Groq(api_key=api_key)
        prompt = (
            "Write a 'Related Work' section (academic tone, 4-7 paragraphs, grouped by "
            "sub-theme) for a paper on the topic below, citing the provided papers using "
            "their exact bracketed keys in plain [key] format — NOT LaTeX \\cite{} syntax, "
            "just the literal bracket, e.g. '...as shown in [smith2023]...'. This avoids "
            "backslash characters that break JSON output; the person pasting this into "
            "LaTeX can find-and-replace [key] with \\cite{key} themselves in one step.\n\n"
            f"TOPIC: {topic}\n\nPAPERS (cite using the bracketed key shown):\n{papers_block}\n\n"
            "Every paper listed must be cited at least once. Do not invent findings not "
            "supported by the title/abstract given. Return pure JSON only, with NO backslash "
            "characters anywhere in the output:\n"
            '{"related_work_section":"full section text with [key] citations inline",'
            '"citation_keys_used":["key1","key2"]}'
        )
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role":"user","content":prompt}],
            temperature=0.3, max_tokens=2500
        )
        raw = response.choices[0].message.content.strip()
        raw = re.sub(r'^```[a-z]*\n?','',raw)
        raw = re.sub(r'\n?```$','',raw)
        # Safety net: strip any invalid backslash escapes the model still
        # produces despite instructions (e.g. stray LaTeX in math notation)
        raw = re.sub(r'\\(?!["\\bfnrtu/]|u[0-9a-fA-F]{4})', '', raw)
        result = json.loads(raw)
        result['bibtex_keys_note'] = 'Citation keys match the keys in your BibTeX export — download both together. Citations are in [key] format; find-and-replace with \\cite{key} for LaTeX.'
        return jsonify(result)
    except json.JSONDecodeError as e:
        return jsonify({'error': f'AI returned malformed JSON. Try again. {str(e)}'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def generate_gap_brief_pdf(topic, gap, output_path):
    """One-page, advisor-meeting-ready PDF for a single gap: the ask, the
    evidence, the plan — designed to be printed or screen-shared in a
    5-minute conversation, not read as a full report."""
    doc = SimpleDocTemplate(output_path, pagesize=A4,
                            rightMargin=0.85*inch, leftMargin=0.85*inch,
                            topMargin=0.85*inch, bottomMargin=0.85*inch)
    styles = getSampleStyleSheet()
    story = []
    title_style = ParagraphStyle('T', parent=styles['Title'], fontSize=18, textColor=colors.HexColor('#1a1a2e'), spaceAfter=4, alignment=TA_CENTER)
    meta = ParagraphStyle('M', parent=styles['Normal'], fontSize=9, textColor=colors.HexColor('#666666'), spaceAfter=4, alignment=TA_CENTER)
    h2 = ParagraphStyle('H2', parent=styles['Heading2'], fontSize=12, textColor=colors.HexColor('#0f3460'), spaceBefore=12, spaceAfter=4)
    body = ParagraphStyle('B', parent=styles['Normal'], fontSize=10.5, leading=15, spaceAfter=6, alignment=TA_JUSTIFY)

    sev_colors = {'critical':'#ff4757','major':'#ffa502','moderate':'#2ed573'}
    sev = gap.get('severity','moderate')

    story.append(Paragraph("Research Gap Brief", title_style))
    story.append(Paragraph(f"For advisor discussion &nbsp;|&nbsp; Topic: {topic}", meta))
    story.append(Paragraph(datetime.datetime.now().strftime('%B %d, %Y'), meta))
    story.append(HRFlowable(width="100%", thickness=2, color=colors.HexColor('#0f3460')))
    story.append(Spacer(1, 10))

    story.append(Paragraph(gap.get('gap_title',''), h2))
    conf = gap.get('confidence_score', 'N/A')
    badge = Table([[f"{sev.upper()} PRIORITY", f"{conf}% confidence"]], colWidths=[3*inch, 3*inch])
    badge.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(0,0), colors.HexColor(sev_colors.get(sev,'#999999'))),
        ('BACKGROUND',(1,0),(1,0), colors.HexColor('#0f3460')),
        ('TEXTCOLOR',(0,0),(-1,-1), colors.white),
        ('ALIGN',(0,0),(-1,-1),'CENTER'), ('FONTNAME',(0,0),(-1,-1),'Helvetica-Bold'),
        ('FONTSIZE',(0,0),(-1,-1),10), ('TOPPADDING',(0,0),(-1,-1),8), ('BOTTOMPADDING',(0,0),(-1,-1),8),
    ]))
    story.append(badge)
    story.append(Spacer(1, 10))

    story.append(Paragraph("The Gap", h2))
    story.append(Paragraph(gap.get('description',''), body))
    story.append(Paragraph("Why It Matters", h2))
    story.append(Paragraph(gap.get('why_important',''), body))
    story.append(Paragraph("Proposed Approach", h2))
    story.append(Paragraph(gap.get('suggested_approach',''), body))
    story.append(Paragraph("Expected Contribution", h2))
    story.append(Paragraph(gap.get('estimated_impact',''), body))

    if gap.get('evidence_verified') and gap.get('evidence_quote'):
        story.append(Paragraph("Verified Evidence", h2))
        page_note = f" (page {gap['evidence_page']})" if gap.get('evidence_page') else ""
        story.append(Paragraph(f"\u201c{gap['evidence_quote']}\u201d{page_note} — confirmed present in the uploaded paper.", body))

    evidence_papers = gap.get('evidence_papers', [])
    if evidence_papers:
        story.append(Paragraph("Supporting Literature", h2))
        for p in evidence_papers[:5]:
            story.append(Paragraph(f"• {p}", body))

    story.append(Spacer(1, 14))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor('#cccccc')))
    story.append(Paragraph("Generated by ResearchLens AI — bring this to your next advisor meeting", meta))
    doc.build(story)


@app.route('/api/gap-brief/pdf', methods=['POST'])
@require_auth
def gap_brief_pdf():
    try:
        data = request.get_json(silent=True) or {}
        topic = data.get('topic', '')
        gap = data.get('gap', {})
        if not gap or not gap.get('gap_title'):
            return jsonify({'error': 'No gap data provided'}), 400
        brief_id = str(uuid.uuid4())[:8]
        filename = f"gap_brief_{brief_id}.pdf"
        path = os.path.join(REPORTS_FOLDER, filename)
        generate_gap_brief_pdf(topic, gap, path)
        return send_file(path, as_attachment=True, download_name=f"gap_brief_{brief_id}.pdf", mimetype='application/pdf')
    except Exception as e:
        print(f"[Gap brief PDF] Failed: {e}")
        return jsonify({'error': f'Could not generate brief: {str(e)}'}), 500




# ═══════════════════════════════════════════════════
# REAL-TIME TRACKING: Watch / Unwatch / List / Check
# ═══════════════════════════════════════════════════

@app.route('/api/watch', methods=['POST'])
@require_auth
def watch_topic():
    data = request.get_json()
    topic = data.get('topic','').strip()
    api_key = data.get('api_key','').strip()
    notify_email = data.get('notify_email','').strip()
    if not topic or not api_key:
        return jsonify({'error':'topic and api_key required'}), 400
    try:
        enc_key = encrypt_secret(api_key)  # never persist the raw Groq key
        conn = get_db()
        # NOTE: `topic` still has a table-level UNIQUE constraint from
        # before ownership tracking existed, so two different users
        # watching the same topic string will collide. Scoping this to
        # (topic, owner_uid) needs a small SQLite table rebuild — flagged
        # in SECURITY_FIXES.md as a follow-up, out of scope for the
        # immediate auth/ownership pass.
        existing = conn.execute('SELECT id, owner_uid FROM watched_topics WHERE topic=?', (topic,)).fetchone()
        if existing and existing['owner_uid'] not in (None, '', g.uid):
            conn.close()
            return jsonify({'error': 'This topic is already being watched by another user'}), 409
        if existing:
            conn.execute('UPDATE watched_topics SET active=1, api_key=?, owner_uid=?, notify_email=? WHERE topic=?', (enc_key, g.uid, notify_email, topic))
        else:
            conn.execute(
                'INSERT INTO watched_topics (topic, owner_uid, api_key, notify_email, created_at, last_checked_at) VALUES (?, ?, ?, ?, ?, ?)',
                (topic, g.uid, enc_key, notify_email, datetime.datetime.now().isoformat(), datetime.datetime.now().isoformat())
            )
        conn.commit()
        conn.close()
        email_note = " You'll get an email when it changes." if notify_email else ""
        return jsonify({'success': True, 'message': f'Now watching "{topic}" — auto-checks every 24 hours.{email_note}'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/unwatch', methods=['POST'])
@require_auth
def unwatch_topic():
    data = request.get_json()
    topic = data.get('topic','').strip()
    try:
        conn = get_db()
        result = conn.execute('UPDATE watched_topics SET active=0 WHERE topic=? AND owner_uid=?', (topic, g.uid))
        conn.commit()
        conn.close()
        if result.rowcount == 0:
            return jsonify({'error': 'Not found'}), 404
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def _format_snapshot(s):
    """
    Reads a topic_snapshots row's new_gaps_json field, which may be either:
    - the NEW format: {'new_gaps': [...], 'confidence_shifts': [...]}
    - the OLD format (pre-existing DBs): a flat list of gap objects
    Returns a consistent dict either way so the frontend never has to guess.
    """
    raw = s['new_gaps_json']
    if not raw:
        return {'checked_at': s['checked_at'], 'new_papers_count': s['new_papers_count'], 'new_gaps': [], 'confidence_shifts': []}
    parsed = json.loads(raw)
    if isinstance(parsed, dict):
        new_gaps = parsed.get('new_gaps', [])
        confidence_shifts = parsed.get('confidence_shifts', [])
    else:
        new_gaps = parsed
        confidence_shifts = []
    return {
        'checked_at': s['checked_at'],
        'new_papers_count': s['new_papers_count'],
        'new_gaps': new_gaps,
        'confidence_shifts': confidence_shifts
    }

@app.route('/api/watched-topics', methods=['GET'])
@require_auth
def list_watched():
    try:
        conn = get_db()
        rows = conn.execute('SELECT * FROM watched_topics WHERE active=1 AND owner_uid=? ORDER BY created_at DESC', (g.uid,)).fetchall()
        result = []
        for r in rows:
            snapshots = conn.execute(
                'SELECT * FROM topic_snapshots WHERE watched_topic_id=? ORDER BY checked_at DESC LIMIT 5',
                (r['id'],)
            ).fetchall()
            result.append({
                'id': r['id'],
                'topic': r['topic'],
                'last_checked_at': r['last_checked_at'],
                'last_gap_count': r['last_gap_count'],
                'last_paper_count': r['last_paper_count'],
                'created_at': r['created_at'],
                'snapshot_count': len(snapshots),
                'recent_snapshots': [_format_snapshot(s) for s in snapshots]
            })
        conn.close()
        return jsonify({'watched_topics': result})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def run_topic_check(topic, api_key, watched_id):
    """
    Re-fetch real ArXiv papers, re-run gap analysis, and compare against the
    LAST SNAPSHOT (not just counts) to identify exactly which gaps and papers
    are genuinely new by title/content, not just a positional slice.
    """
    try:
        conn = get_db()
        row = conn.execute('SELECT * FROM watched_topics WHERE id=?', (watched_id,)).fetchone()
        if not row:
            conn.close()
            print(f"[Scheduler] Watched topic id={watched_id} no longer exists, skipping")
            return

        prev_snapshot = conn.execute(
            'SELECT * FROM topic_snapshots WHERE watched_topic_id=? ORDER BY checked_at DESC LIMIT 1',
            (watched_id,)
        ).fetchone()

        prev_gap_titles = set()
        prev_paper_titles = set()
        if prev_snapshot:
            prev_analysis = json.loads(prev_snapshot['analysis_json'])
            prev_gap_titles = {g.get('gap_title','').strip().lower() for g in prev_analysis.get('research_gaps', [])}
            prev_paper_titles = {p.get('title','').strip().lower() for p in prev_analysis.get('papers', [])}
        elif row['last_gap_count'] or row['last_paper_count']:
            # No snapshot yet but watched_topics has baseline counts from initial watch — treat as no prior titles known
            pass

        # Real multi-source fetch — will raise ArxivUnavailableError if all sources unreachable
        papers = fetch_papers_multi_source(topic, max_results=50)
        combined_text = " ".join(p['title']+' '+p['abstract'] for p in papers)
        keywords = extract_keywords(combined_text, top_n=30)
        analysis = analyze_gaps_with_groq(api_key, topic, "", papers, keywords)
        analysis['papers'] = papers  # store alongside for next diff

        new_gaps = analysis.get('research_gaps', [])
        new_gap_count = len(new_gaps)
        new_paper_count = len(papers)

        # Title-based diff (not positional) — this is what actually makes the
        # "new since last check" claim accurate instead of approximate
        current_gap_titles = {g.get('gap_title','').strip().lower(): g for g in new_gaps}
        current_paper_titles = {p.get('title','').strip().lower(): p for p in papers}

        if prev_snapshot:
            truly_new_gap_keys = set(current_gap_titles.keys()) - prev_gap_titles
            truly_new_paper_keys = set(current_paper_titles.keys()) - prev_paper_titles
        else:
            # First-ever check after watching — nothing to diff against, so report 0 "new"
            # (the baseline itself isn't "new", it's the starting point)
            truly_new_gap_keys = set()
            truly_new_paper_keys = set()

        newly_found_gaps = [current_gap_titles[k] for k in truly_new_gap_keys]
        new_paper_count_diff = len(truly_new_paper_keys)

        # Confidence-shift tracking: for gaps that existed in BOTH the previous
        # and current snapshot, record how their confidence score changed
        confidence_shifts = []
        if prev_snapshot:
            prev_analysis_for_shift = json.loads(prev_snapshot['analysis_json'])
            prev_gap_by_title = {g.get('gap_title','').strip().lower(): g for g in prev_analysis_for_shift.get('research_gaps', [])}
            persisted_keys = set(current_gap_titles.keys()) & prev_gap_titles
            for k in persisted_keys:
                old_conf = prev_gap_by_title.get(k, {}).get('confidence_score', 0)
                new_conf = current_gap_titles[k].get('confidence_score', 0)
                if old_conf != new_conf:
                    confidence_shifts.append({
                        'gap_title': current_gap_titles[k].get('gap_title',''),
                        'old_confidence': old_conf,
                        'new_confidence': new_conf,
                        'direction': 'up' if new_conf > old_conf else 'down'
                    })

        conn.execute(
            'INSERT INTO topic_snapshots (watched_topic_id, analysis_json, new_gaps_json, new_papers_count, checked_at) VALUES (?, ?, ?, ?, ?)',
            (watched_id, json.dumps(analysis), json.dumps({'new_gaps': newly_found_gaps, 'confidence_shifts': confidence_shifts}), new_paper_count_diff, datetime.datetime.now().isoformat())
        )
        conn.execute(
            'UPDATE watched_topics SET last_checked_at=?, last_gap_count=?, last_paper_count=? WHERE id=?',
            (datetime.datetime.now().isoformat(), new_gap_count, new_paper_count, watched_id)
        )
        conn.commit()
        conn.close()
        print(f"[Scheduler] Checked '{topic}': {new_gap_count} total gaps, {new_paper_count} total papers — {len(newly_found_gaps)} genuinely NEW gaps, {new_paper_count_diff} genuinely NEW papers since last check")

        if prev_snapshot and (newly_found_gaps or new_paper_count_diff > 0):
            notify_email = row['notify_email'] if 'notify_email' in row.keys() else None
            if notify_email:
                send_alert_email(notify_email, topic, newly_found_gaps, new_paper_count_diff)
    except ArxivUnavailableError as e:
        print(f"[Scheduler] ArXiv unreachable while checking '{topic}': {e}")
    except Exception as e:
        print(f"[Scheduler] Error checking '{topic}': {e}")

orchestrator.register(MonitoringAgent(run_topic_check))

@app.route('/api/check-now/<int:watched_id>', methods=['POST'])
@require_auth
def check_now(watched_id):
    try:
        conn = get_db()
        row = conn.execute('SELECT * FROM watched_topics WHERE id=?', (watched_id,)).fetchone()
        conn.close()
        if not row or row['owner_uid'] != g.uid:
            return jsonify({'error':'Watched topic not found'}), 404
        run_topic_check(row['topic'], decrypt_secret(row['api_key']), watched_id)
        return jsonify({'success': True, 'message': 'Check completed'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def scheduled_check_all():
    """Runs daily — re-checks every active watched topic."""
    try:
        conn = get_db()
        rows = conn.execute('SELECT * FROM watched_topics WHERE active=1').fetchall()
        conn.close()
        print(f"[Scheduler] Running daily check for {len(rows)} watched topics...")
        for r in rows:
            run_topic_check(r['topic'], decrypt_secret(r['api_key']), r['id'])
    except Exception as e:
        print(f"[Scheduler] Batch check error: {e}")


# ═══════════════════════════════════════════════════
# COLLABORATION: Lightweight join-code workspaces
# ═══════════════════════════════════════════════════
# See the note on the schema above: join_code = shared secret, not a real
# per-user permission system. Good enough for a trusted lab, not for
# adversarial confidentiality.

import random, string as _string

def _generate_join_code():
    return ''.join(random.choices(_string.ascii_uppercase + _string.digits, k=6))

@app.route('/api/workspace/create', methods=['POST'])
@require_auth
def create_workspace():
    try:
        data = request.get_json() or {}
        name = data.get('name', '').strip()
        display_name = data.get('display_name', '').strip()
        if not name or not display_name:
            return jsonify({'error': 'Workspace name and your display name are required'}), 400
        conn = get_db()
        join_code = _generate_join_code()
        # Extremely unlikely, but guard against a join_code collision anyway
        while conn.execute('SELECT id FROM workspaces WHERE join_code=?', (join_code,)).fetchone():
            join_code = _generate_join_code()
        cur = conn.execute(
            'INSERT INTO workspaces (name, join_code, created_at) VALUES (?, ?, ?)',
            (name, join_code, datetime.datetime.now().isoformat())
        )
        workspace_id = cur.lastrowid
        conn.execute(
            'INSERT INTO workspace_members (workspace_id, display_name, joined_at) VALUES (?, ?, ?)',
            (workspace_id, display_name, datetime.datetime.now().isoformat())
        )
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'join_code': join_code, 'workspace_name': name})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/workspace/join', methods=['POST'])
@require_auth
def join_workspace():
    try:
        data = request.get_json() or {}
        join_code = data.get('join_code', '').strip().upper()
        display_name = data.get('display_name', '').strip()
        if not join_code or not display_name:
            return jsonify({'error': 'Join code and display name are required'}), 400
        conn = get_db()
        ws = conn.execute('SELECT * FROM workspaces WHERE join_code=?', (join_code,)).fetchone()
        if not ws:
            conn.close()
            return jsonify({'error': 'No workspace found with that join code'}), 404
        existing = conn.execute(
            'SELECT id FROM workspace_members WHERE workspace_id=? AND display_name=?',
            (ws['id'], display_name)
        ).fetchone()
        if not existing:
            conn.execute(
                'INSERT INTO workspace_members (workspace_id, display_name, joined_at) VALUES (?, ?, ?)',
                (ws['id'], display_name, datetime.datetime.now().isoformat())
            )
            conn.commit()
        members = conn.execute('SELECT display_name, joined_at FROM workspace_members WHERE workspace_id=?', (ws['id'],)).fetchall()
        conn.close()
        return jsonify({
            'success': True,
            'workspace_name': ws['name'],
            'join_code': join_code,
            'members': [dict(m) for m in members]
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def _get_workspace_or_404(conn, join_code):
    return conn.execute('SELECT * FROM workspaces WHERE join_code=?', (join_code.strip().upper(),)).fetchone()

@app.route('/api/workspace/<join_code>/contribute', methods=['POST'])
@require_auth
def workspace_contribute(join_code):
    """
    Cross-referencing: adds papers from a member's analysis into the shared
    workspace library, deduplicated by normalized title, so the lab's
    collective literature review builds over time instead of resetting
    per person.
    """
    try:
        data = request.get_json() or {}
        display_name = data.get('display_name', '').strip()
        papers = data.get('papers', [])
        topic = data.get('topic', '')
        if not display_name or not papers:
            return jsonify({'error': 'display_name and papers are required'}), 400
        conn = get_db()
        ws = _get_workspace_or_404(conn, join_code)
        if not ws:
            conn.close()
            return jsonify({'error': 'Workspace not found'}), 404
        added, skipped = 0, 0
        for p in papers:
            norm = _normalize_title(p.get('title', ''))
            if not norm:
                continue
            existing = conn.execute(
                'SELECT id FROM workspace_papers WHERE workspace_id=? AND normalized_title=?',
                (ws['id'], norm)
            ).fetchone()
            if existing:
                skipped += 1
                continue
            conn.execute(
                'INSERT INTO workspace_papers (workspace_id, title, normalized_title, abstract, year, authors_json, source, topic, contributed_by, added_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                (ws['id'], p.get('title',''), norm, p.get('abstract',''), p.get('year',''),
                 json.dumps(p.get('authors', [])), p.get('source',''), topic, display_name,
                 datetime.datetime.now().isoformat())
            )
            added += 1
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'added': added, 'skipped_duplicates': skipped})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def _normalize_title_shared(t):
    return _normalize_title(t)

@app.route('/api/workspace/<join_code>/library', methods=['GET'])
@require_auth
def workspace_library(join_code):
    try:
        conn = get_db()
        ws = _get_workspace_or_404(conn, join_code)
        if not ws:
            conn.close()
            return jsonify({'error': 'Workspace not found'}), 404
        rows = conn.execute(
            'SELECT title, abstract, year, authors_json, source, topic, contributed_by, added_at FROM workspace_papers WHERE workspace_id=? ORDER BY added_at DESC',
            (ws['id'],)
        ).fetchall()
        conn.close()
        papers = []
        for r in rows:
            d = dict(r)
            d['authors'] = json.loads(d.pop('authors_json') or '[]')
            papers.append(d)
        return jsonify({'papers': papers, 'count': len(papers)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/workspace/<join_code>/gap/comment', methods=['POST'])
@require_auth
def add_gap_comment(join_code):
    try:
        data = request.get_json() or {}
        display_name = data.get('display_name', '').strip()
        gap_title = data.get('gap_title', '').strip()
        comment_text = data.get('comment', '').strip()
        if not all([display_name, gap_title, comment_text]):
            return jsonify({'error': 'display_name, gap_title, and comment are required'}), 400
        conn = get_db()
        ws = _get_workspace_or_404(conn, join_code)
        if not ws:
            conn.close()
            return jsonify({'error': 'Workspace not found'}), 404
        conn.execute(
            'INSERT INTO gap_comments (workspace_id, gap_title, author_name, comment_text, created_at) VALUES (?,?,?,?,?)',
            (ws['id'], gap_title, display_name, comment_text, datetime.datetime.now().isoformat())
        )
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/workspace/<join_code>/gap/comments', methods=['GET'])
@require_auth
def get_gap_comments(join_code):
    try:
        gap_title = request.args.get('gap_title', '').strip()
        conn = get_db()
        ws = _get_workspace_or_404(conn, join_code)
        if not ws:
            conn.close()
            return jsonify({'error': 'Workspace not found'}), 404
        rows = conn.execute(
            'SELECT id, author_name, comment_text, created_at FROM gap_comments WHERE workspace_id=? AND gap_title=? ORDER BY created_at ASC',
            (ws['id'], gap_title)
        ).fetchall()
        conn.close()
        return jsonify({'comments': [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/workspace/<join_code>/gap/comment/<int:comment_id>', methods=['DELETE'])
@require_auth
def delete_gap_comment(join_code, comment_id):
    try:
        conn = get_db()
        ws = _get_workspace_or_404(conn, join_code)
        if not ws:
            conn.close()
            return jsonify({'error': 'Workspace not found'}), 404
        row = conn.execute(
            'SELECT id FROM gap_comments WHERE id=? AND workspace_id=?', (comment_id, ws['id'])
        ).fetchone()
        if not row:
            conn.close()
            return jsonify({'error': 'Comment not found'}), 404
        conn.execute('DELETE FROM gap_comments WHERE id=? AND workspace_id=?', (comment_id, ws['id']))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/workspace/<join_code>/gap/assign', methods=['POST'])
@require_auth
def assign_gap(join_code):
    try:
        data = request.get_json() or {}
        display_name = data.get('display_name', '').strip()  # who's doing the assigning
        gap_title = data.get('gap_title', '').strip()
        assigned_to = data.get('assigned_to', '').strip()
        if not all([gap_title, assigned_to]):
            return jsonify({'error': 'gap_title and assigned_to are required'}), 400
        conn = get_db()
        ws = _get_workspace_or_404(conn, join_code)
        if not ws:
            conn.close()
            return jsonify({'error': 'Workspace not found'}), 404
        conn.execute(
            'INSERT INTO gap_assignments (workspace_id, gap_title, assigned_to, assigned_by, assigned_at) VALUES (?,?,?,?,?)',
            (ws['id'], gap_title, assigned_to, display_name, datetime.datetime.now().isoformat())
        )
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/workspace/<join_code>/gap/assignments', methods=['GET'])
@require_auth
def get_gap_assignments(join_code):
    try:
        conn = get_db()
        ws = _get_workspace_or_404(conn, join_code)
        if not ws:
            conn.close()
            return jsonify({'error': 'Workspace not found'}), 404
        # Latest assignment per gap_title only
        rows = conn.execute(
            '''SELECT gap_title, assigned_to, assigned_by, assigned_at FROM gap_assignments
               WHERE workspace_id=? AND id IN (
                   SELECT MAX(id) FROM gap_assignments WHERE workspace_id=? GROUP BY gap_title
               )''',
            (ws['id'], ws['id'])
        ).fetchall()
        conn.close()
        return jsonify({'assignments': [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/workspace/<join_code>/dashboard', methods=['GET'])
@require_auth
def workspace_dashboard(join_code):
    """
    Institutional/department-style aggregate view: what has this group
    collectively looked at, what's underserved (low-comment / unassigned
    gaps), where's the volume — the "spot underserved areas" ask.
    """
    try:
        conn = get_db()
        ws = _get_workspace_or_404(conn, join_code)
        if not ws:
            conn.close()
            return jsonify({'error': 'Workspace not found'}), 404

        members = conn.execute('SELECT display_name, joined_at FROM workspace_members WHERE workspace_id=?', (ws['id'],)).fetchall()
        papers = conn.execute('SELECT title, abstract, topic, contributed_by FROM workspace_papers WHERE workspace_id=?', (ws['id'],)).fetchall()
        comments = conn.execute('SELECT gap_title, author_name FROM gap_comments WHERE workspace_id=?', (ws['id'],)).fetchall()
        assignments = conn.execute(
            '''SELECT gap_title, assigned_to FROM gap_assignments
               WHERE workspace_id=? AND id IN (SELECT MAX(id) FROM gap_assignments WHERE workspace_id=? GROUP BY gap_title)''',
            (ws['id'], ws['id'])
        ).fetchall()

        # Topic coverage: which topics has this group explored, and how much
        topic_counts = Counter(p['topic'] for p in papers if p['topic'])
        # Keyword frequency across the shared library — a cheap proxy for
        # "what is this group actually spending its attention on"
        combined_text = ' '.join((p['title'] or '') + ' ' + (p['abstract'] or '') for p in papers)
        top_keywords = extract_keywords(combined_text, top_n=15) if combined_text.strip() else []
        # Contribution breakdown per member
        contributor_counts = Counter(p['contributed_by'] for p in papers if p['contributed_by'])
        # Gaps with no comments and no assignee — the "underserved areas"
        commented_gaps = set(c['gap_title'] for c in comments)
        assigned_gaps = set(a['gap_title'] for a in assignments)

        conn.close()
        return jsonify({
            'workspace_name': ws['name'],
            'member_count': len(members),
            'members': [dict(m) for m in members],
            'paper_count': len(papers),
            'topic_coverage': [{'topic': t, 'papers': c} for t, c in topic_counts.most_common(10)],
            'top_keywords': top_keywords,
            'contributor_breakdown': [{'member': m, 'papers_contributed': c} for m, c in contributor_counts.most_common()],
            'total_gap_discussions': len(commented_gaps),
            'total_gap_assignments': len(assigned_gaps),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ═══════════════════════════════════════════════════
# INSTITUTIONAL: Thesis / proposal novelty pre-check
# ═══════════════════════════════════════════════════
@app.route('/api/thesis-check', methods=['POST'])
@require_auth
def thesis_check():
    """
    'Has this already been done?' — checks a proposed contribution against
    live literature BEFORE submission, so a student (or a committee) finds
    out early rather than in a defense. Reuses the same multi-source fetch
    and honest-failure behavior as the main analysis flow.
    """
    api_key = request.form.get('api_key', '').strip()
    topic = request.form.get('topic', '').strip()
    proposed_contribution = request.form.get('proposed_contribution', '').strip()
    if not api_key or not topic or not proposed_contribution:
        return jsonify({'error': 'api_key, topic, and proposed_contribution are required'}), 400

    try:
        papers = fetch_papers_multi_source(topic, max_results=40)
    except ArxivUnavailableError as e:
        return jsonify({'error': str(e), 'error_type': 'arxiv_unreachable', 'source_label': getattr(e, 'source_label', 'ArXiv')}), 503

    try:
        client = Groq(api_key=api_key)
        paper_titles = "\n".join([f"- {p['title']} ({p['year']}): {p['abstract'][:200]}" for p in papers[:20]])
        prompt = (
            "You are a thesis committee member checking whether a proposed research "
            "contribution is genuinely novel, given the current literature below. Be "
            "honest and specific — do not soften a real overlap to be encouraging.\n\n"
            f"PROPOSED CONTRIBUTION: {proposed_contribution}\n\n"
            f"CURRENT LITERATURE FOUND:\n{paper_titles}\n\n"
            "Return pure JSON only:\n"
            '{"verdict":"novel/partially_covered/already_done",'
            '"confidence":85,'
            '"reasoning":"2-4 sentences explaining the verdict, citing specific papers by title if there is overlap",'
            '"closest_matches":[{"paper":"exact title from the list above","overlap_description":"what specifically overlaps"}],'
            '"differentiation_suggestions":["if partially_covered or already_done, concrete ways to differentiate the contribution"]}'
        )
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role":"user","content":prompt}],
            temperature=0.2, max_tokens=1200
        )
        raw = response.choices[0].message.content.strip()
        raw = re.sub(r'^```[a-z]*\n?','',raw)
        raw = re.sub(r'\n?```$','',raw)
        raw = re.sub(r'\\(?!["\\bfnrtu/]|u[0-9a-fA-F]{4})', '', raw)
        result = json.loads(raw)
        result['papers_checked_against'] = len(papers)
        return jsonify(result)
    except json.JSONDecodeError as e:
        return jsonify({'error': f'AI returned malformed JSON. Try again. {str(e)}'}), 500
    except Exception as e:
        return jsonify({'error': f'Novelty check failed: {str(e)}'}), 500


# ═══════════════════════════════════════════════════
# COMMUNITY: Opt-in public gap-sharing layer
# ═══════════════════════════════════════════════════
@app.route('/api/community/share-gap', methods=['POST'])
@require_auth
def share_gap_publicly():
    try:
        data = request.get_json() or {}
        if not data.get('consent'):
            return jsonify({'error': 'Explicit consent required to share publicly'}), 400
        gap_title = data.get('gap_title', '').strip()
        if not gap_title:
            return jsonify({'error': 'gap_title is required'}), 400
        conn = get_db()
        conn.execute(
            'INSERT INTO public_gaps (gap_title, description, topic, severity, contributor_name, owner_uid, shared_at) VALUES (?,?,?,?,?,?,?)',
            (gap_title, data.get('description',''), data.get('topic',''), data.get('severity','moderate'),
             (data.get('contributor_name') or 'Anonymous').strip()[:60], g.uid, datetime.datetime.now().isoformat())
        )
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'Gap shared to the community feed'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/community/gaps', methods=['GET'])
@require_auth
def list_community_gaps():
    try:
        search = request.args.get('search', '').strip().lower()
        limit = min(int(request.args.get('limit', 30)), 100)
        conn = get_db()
        if search:
            like = f"%{search}%"
            rows = conn.execute(
                'SELECT * FROM public_gaps WHERE lower(gap_title) LIKE ? OR lower(topic) LIKE ? OR lower(description) LIKE ? ORDER BY shared_at DESC LIMIT ?',
                (like, like, like, limit)
            ).fetchall()
        else:
            rows = conn.execute('SELECT * FROM public_gaps ORDER BY shared_at DESC LIMIT ?', (limit,)).fetchall()
        conn.close()
        return jsonify({'gaps': [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/community/gaps/<int:gap_id>/upvote', methods=['POST'])
@require_auth
def upvote_community_gap(gap_id):
    try:
        conn = get_db()
        conn.execute('UPDATE public_gaps SET upvotes = upvotes + 1 WHERE id=?', (gap_id,))
        conn.commit()
        row = conn.execute('SELECT upvotes FROM public_gaps WHERE id=?', (gap_id,)).fetchone()
        conn.close()
        if not row:
            return jsonify({'error': 'Gap not found'}), 404
        return jsonify({'success': True, 'upvotes': row['upvotes']})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/community/gaps/<int:gap_id>', methods=['DELETE'])
@require_auth
def delete_community_gap(gap_id):
    try:
        conn = get_db()
        row = conn.execute('SELECT id, owner_uid FROM public_gaps WHERE id=?', (gap_id,)).fetchone()
        if not row:
            conn.close()
            return jsonify({'error': 'Gap not found'}), 404
        # Older rows shared before ownership tracking existed have no
        # owner_uid — nobody can delete those via this endpoint; an admin
        # would need to do it directly. Rows shared after this fix can
        # only be deleted by the uid that shared them.
        if row['owner_uid'] != g.uid:
            conn.close()
            return jsonify({'error': 'Not authorized to delete this gap'}), 403
        conn.execute('DELETE FROM public_gaps WHERE id=?', (gap_id,))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ═══════════════════════════════════════════════════
# EVALUATION INFRASTRUCTURE
# ═══════════════════════════════════════════════════
# The real first step toward a gold-standard accuracy benchmark: let a
# human (you, or anyone you show this to) mark whether a detected gap
# actually holds up. On its own, an empty table proves nothing — this only
# becomes meaningful evaluation infrastructure once real people with real
# domain knowledge rate real gaps over time. Nothing here is simulated or
# pre-populated with fake ratings.

@app.route('/api/gap-feedback', methods=['POST'])
@require_auth
def record_gap_feedback():
    try:
        data = request.get_json() or {}
        gap_title = data.get('gap_title', '').strip()
        verdict = data.get('verdict', '').strip()  # 'valid' | 'invalid' | 'unsure'
        if not gap_title or verdict not in ('valid', 'invalid', 'unsure'):
            return jsonify({'error': "gap_title and a verdict of 'valid', 'invalid', or 'unsure' are required"}), 400
        conn = get_db()
        conn.execute(
            '''INSERT INTO gap_feedback
               (topic, gap_title, severity, confidence_score, evidence_verified, verdict, rater_name, notes, rated_at)
               VALUES (?,?,?,?,?,?,?,?,?)''',
            (data.get('topic', ''), gap_title, data.get('severity', ''),
             data.get('confidence_score'), 1 if data.get('evidence_verified') else 0,
             verdict, data.get('rater_name', '').strip()[:60] or 'Anonymous',
             data.get('notes', '').strip(), datetime.datetime.now().isoformat())
        )
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/eval/accuracy-summary', methods=['GET'])
@require_auth
def accuracy_summary():
    """
    The actual evaluation dashboard: real counts from real ratings, not a
    marketing number. Returns honest 'not enough data yet' state when the
    sample is too small to mean anything, instead of a misleading
    percentage computed from 2 ratings.
    """
    try:
        conn = get_db()
        rows = conn.execute('SELECT * FROM gap_feedback ORDER BY rated_at DESC').fetchall()
        conn.close()
        total = len(rows)
        if total == 0:
            return jsonify({
                'total_ratings': 0,
                'message': 'No feedback recorded yet — this dashboard only becomes meaningful once real gaps get rated.'
            })

        verdict_counts = Counter(r['verdict'] for r in rows)
        valid_rate = round(100 * verdict_counts.get('valid', 0) / total, 1)

        # Does the model's own confidence score actually correlate with
        # being right? This is the single most important number for
        # defending (or not defending) accuracy claims.
        by_severity = {}
        for sev in ('critical', 'major', 'moderate'):
            sev_rows = [r for r in rows if r['severity'] == sev]
            if sev_rows:
                sev_valid = sum(1 for r in sev_rows if r['verdict'] == 'valid')
                by_severity[sev] = {'total': len(sev_rows), 'valid_rate': round(100*sev_valid/len(sev_rows), 1)}

        confidence_buckets = {'high (80-100)': [], 'medium (60-79)': [], 'low (<60)': []}
        for r in rows:
            c = r['confidence_score']
            if c is None:
                continue
            bucket = 'high (80-100)' if c >= 80 else 'medium (60-79)' if c >= 60 else 'low (<60)'
            confidence_buckets[bucket].append(r['verdict'] == 'valid')
        confidence_calibration = {
            b: {'total': len(v), 'valid_rate': round(100*sum(v)/len(v), 1) if v else None}
            for b, v in confidence_buckets.items() if v
        }

        evidence_rows = [r for r in rows if r['confidence_score'] is not None]
        verified_rows = [r for r in rows if r['evidence_verified'] == 1]
        unverified_rows = [r for r in rows if r['evidence_verified'] == 0]

        return jsonify({
            'total_ratings': total,
            'overall_valid_rate': valid_rate,
            'verdict_breakdown': dict(verdict_counts),
            'by_severity': by_severity,
            'confidence_calibration': confidence_calibration,
            'evidence_verified_valid_rate': (
                round(100*sum(1 for r in verified_rows if r['verdict']=='valid')/len(verified_rows), 1)
                if verified_rows else None
            ),
            'evidence_unverified_valid_rate': (
                round(100*sum(1 for r in unverified_rows if r['verdict']=='valid')/len(unverified_rows), 1)
                if unverified_rows else None
            ),
            'note': 'Small sample sizes are not statistically meaningful — treat any rate below ~30 ratings as directional only, not a defensible accuracy claim.',
            'recent_feedback': [dict(r) for r in rows[:20]],
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# Start the background scheduler (daily checks)
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    scheduler = BackgroundScheduler()
    scheduler.add_job(scheduled_check_all, 'interval', hours=24, id='daily_topic_check')
    scheduler.start()
    print("[Scheduler] Started — watched topics will auto-check every 24 hours")
except Exception as e:
    print(f"[Scheduler] Could not start: {e}")

if __name__ == '__main__':
    import os
    debug_mode = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=debug_mode, host='0.0.0.0', port=port)