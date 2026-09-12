"""
Publication Intelligence layer (Domain -> Sub-domain -> Society ->
Publisher -> Journal/Conference).

This module is additive: it creates its own tables, seeds them
idempotently from `publication_data.py`, and exposes its own routes
under a Blueprint. It does not modify any existing `analyses` /
`watched_topics` / workspace table or route.

NO FAKE DATA POLICY
--------------------
Static, durable facts (domain names, society/publisher/venue identity)
are safe to seed from general knowledge. Time-sensitive, database-
dependent facts (journal quartile, Impact Factor, CiteScore, SJR,
indexing status, acceptance rate) are NEVER fabricated here. This app
has no live connection to Scopus / Web of Science / JCR from this
environment, so every such field is stored as NULL and always
rendered to the user as "Not verified" — see `NOT_VERIFIED` below and
`metrics_payload()`. If a real metadata provider (OpenAlex, Crossref,
Scopus API, etc.) is wired in later, populate these columns from that
provider's response and stamp `source` / `retrieved_at`, rather than
loosening this rule.
"""
import sqlite3
from flask import Blueprint, jsonify, request, g

from auth import require_auth
from publication_data import DOMAINS, SOCIETIES, PUBLISHERS, VENUES, PRIMARY_DOMAINS, PRIMARY_TO_SECONDARY, PRIMARY_DOMAIN_SOURCES, SPECIFIC_VENUES_BY_FIELD

pub_bp = Blueprint('publications', __name__)

NOT_VERIFIED = "Not verified"

# ─── Schema ────────────────────────────────────────────────────────────


def init_publication_tables(conn: sqlite3.Connection):
    conn.execute('''CREATE TABLE IF NOT EXISTS domains (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        category TEXT NOT NULL,
        parent_name TEXT,
        keywords TEXT
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS societies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        description TEXT,
        website TEXT
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS publishers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        website TEXT
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS domain_societies (
        domain_id INTEGER NOT NULL,
        society_id INTEGER NOT NULL,
        PRIMARY KEY (domain_id, society_id)
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS domain_publishers (
        domain_id INTEGER NOT NULL,
        publisher_id INTEGER NOT NULL,
        PRIMARY KEY (domain_id, publisher_id)
    )''')
    # A venue (journal or conference). Metric columns are always NULL at
    # seed time — see module docstring. `verified_at` stays NULL until a
    # real provider call populates the metric columns.
    conn.execute('''CREATE TABLE IF NOT EXISTS venues (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        venue_type TEXT NOT NULL,           -- 'journal' | 'conference'
        acronym TEXT,
        publisher_name TEXT,
        society_name TEXT,
        quartile TEXT,                      -- e.g. 'Q1' — NULL unless verified
        quartile_database TEXT,             -- e.g. 'Scopus' / 'Web of Science'
        quartile_year INTEGER,
        impact_factor REAL,
        citescore REAL,
        indexing TEXT,                      -- comma-separated, e.g. 'Scopus,WoS'
        website TEXT,
        source TEXT,                        -- where a metric came from, if any
        verified_at TEXT
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS venue_domains (
        venue_id INTEGER NOT NULL,
        domain_id INTEGER NOT NULL,
        PRIMARY KEY (venue_id, domain_id)
    )''')
    # Traceability: which domains a past analysis/query was classified
    # into, so the Knowledge Graph and Publication Explorer can link back.
    conn.execute('''CREATE TABLE IF NOT EXISTS query_domain_classifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        analysis_id INTEGER,
        owner_uid TEXT,
        query_text TEXT NOT NULL,
        primary_domain TEXT,
        primary_confidence REAL,
        secondary_domains_json TEXT,
        low_confidence INTEGER DEFAULT 0,
        created_at TEXT
    )''')
    conn.commit()


def seed_publication_data(conn: sqlite3.Connection):
    """Idempotent — safe to call on every startup (INSERT OR IGNORE by
    unique name, like the rest of the app's migration pattern)."""
    for name, category, parent, keywords in DOMAINS:
        conn.execute(
            'INSERT OR IGNORE INTO domains (name, category, parent_name, keywords) VALUES (?,?,?,?)',
            (name, category, parent, keywords)
        )
    for name, desc, website, _cats in SOCIETIES:
        conn.execute(
            'INSERT OR IGNORE INTO societies (name, description, website) VALUES (?,?,?)',
            (name, desc, website)
        )
    for name, website, _cats in PUBLISHERS:
        conn.execute(
            'INSERT OR IGNORE INTO publishers (name, website) VALUES (?,?)',
            (name, website)
        )
    conn.commit()

    # Link domains <-> societies/publishers by category
    domain_rows = conn.execute('SELECT id, category FROM domains').fetchall()
    for _name, _desc, _website, cats in SOCIETIES:
        soc_id = conn.execute('SELECT id FROM societies WHERE name=?', (_name,)).fetchone()[0]
        for d in domain_rows:
            if d['category'] in cats:
                conn.execute(
                    'INSERT OR IGNORE INTO domain_societies (domain_id, society_id) VALUES (?,?)',
                    (d['id'], soc_id)
                )
    for _name, _website, cats in PUBLISHERS:
        pub_id = conn.execute('SELECT id FROM publishers WHERE name=?', (_name,)).fetchone()[0]
        for d in domain_rows:
            if d['category'] in cats:
                conn.execute(
                    'INSERT OR IGNORE INTO domain_publishers (domain_id, publisher_id) VALUES (?,?)',
                    (d['id'], pub_id)
                )
    conn.commit()

    # Venues + their domain links (matched by domain *name*, not category —
    # venues are specific to sub-domains, unlike society/publisher mapping)
    for name, vtype, acronym, domain_names, publisher, society in VENUES:
        conn.execute(
            '''INSERT OR IGNORE INTO venues
               (name, venue_type, acronym, publisher_name, society_name)
               VALUES (?,?,?,?,?)''',
            (name, vtype, acronym, publisher, society)
        )
        venue_id = conn.execute('SELECT id FROM venues WHERE name=?', (name,)).fetchone()[0]
        for dn in domain_names:
            drow = conn.execute('SELECT id FROM domains WHERE name=?', (dn,)).fetchone()
            if drow:
                conn.execute(
                    'INSERT OR IGNORE INTO venue_domains (venue_id, domain_id) VALUES (?,?)',
                    (venue_id, drow['id'])
                )
    conn.commit()


# ─── Serialization helpers ─────────────────────────────────────────────

def _domain_row(r):
    return {'id': r['id'], 'name': r['name'], 'category': r['category'],
            'parent': r['parent_name']}


def _society_row(r):
    return {'id': r['id'], 'name': r['name'], 'description': r['description'], 'website': r['website']}


def _publisher_row(r):
    return {'id': r['id'], 'name': r['name'], 'website': r['website']}


def _venue_row(r):
    return {
        'id': r['id'], 'name': r['name'], 'venue_type': r['venue_type'], 'acronym': r['acronym'],
        'publisher': r['publisher_name'], 'society': r['society_name'],
        'quartile': r['quartile'] or NOT_VERIFIED,
        'quartile_database': r['quartile_database'] or NOT_VERIFIED,
        'quartile_year': r['quartile_year'],
        'impact_factor': r['impact_factor'] if r['impact_factor'] is not None else NOT_VERIFIED,
        'citescore': r['citescore'] if r['citescore'] is not None else NOT_VERIFIED,
        'indexing': r['indexing'] or NOT_VERIFIED,
        'website': r['website'],
        'metrics_source': r['source'] or 'No live metadata provider connected — metrics not verified',
        'verified_at': r['verified_at'],
    }


# ─── Classifier (TF-IDF over domain name+keywords, no network) ────────

def classify_query(conn: sqlite3.Connection, query_text: str, top_n: int = 8):
    from agents.textsim import tokenize, tfidf_vectors, cosine

    rows = conn.execute('SELECT id, name, category, parent_name, keywords FROM domains').fetchall()
    docs = [f"{r['name']} {r['name']} {r['keywords'] or ''}" for r in rows]
    if not docs:
        return {'primary_domain': None, 'secondary_domains': [], 'low_confidence': True}

    vectors = tfidf_vectors([query_text] + docs)
    qv = vectors[0]
    scored = []
    q_tokens = set(tokenize(query_text))
    for i, r in enumerate(rows):
        sim = cosine(qv, vectors[i + 1])
        # Small boost for exact keyword/name token overlap, so short
        # queries like "Kannada NLP" reliably surface "Kannada Linguistics"
        # even though TF-IDF alone is noisy on very short documents.
        name_tokens = set(tokenize(f"{r['name']} {r['keywords'] or ''}"))
        overlap = len(q_tokens & name_tokens)
        score = sim + 0.08 * overlap
        if score > 0:
            scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    if not scored:
        return {'primary_domain': None, 'secondary_domains': [], 'low_confidence': True,
                'message': 'No confident domain match; try adding a technique or application keyword.'}

    max_score = scored[0][0]
    candidates = []
    for score, r in scored[:top_n]:
        confidence = round(min(score / max_score, 1.0) * (0.95 if score == max_score else 0.9), 2)
        candidates.append({
            'domain_id': r['id'], 'name': r['name'], 'category': r['category'],
            'parent': r['parent_name'], 'confidence': confidence,
        })

    primary = candidates[0]
    secondary = [c for c in candidates[1:] if c['confidence'] >= 0.35]
    low_confidence = primary['confidence'] < 0.55 or (
        len(candidates) > 1 and (primary['confidence'] - candidates[1]['confidence']) < 0.08
    )
    return {
        'primary_domain': primary,
        'secondary_domains': secondary,
        'low_confidence': low_confidence,
        'message': 'Multiple possible domains — consider selecting the intended one.' if low_confidence else None,
    }


# ─── Routes ─────────────────────────────────────────────────────────────

def register_publication_routes(app, get_db):

    @pub_bp.route('/api/domains', methods=['GET'])
    def list_domains():
        conn = get_db()
        category = request.args.get('category')
        search = request.args.get('q', '').strip()
        sql = 'SELECT * FROM domains'
        params = []
        clauses = []
        if category:
            clauses.append('category = ?')
            params.append(category)
        if search:
            clauses.append('name LIKE ?')
            params.append(f'%{search}%')
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY category, name'
        rows = conn.execute(sql, params).fetchall()
        categories = [r['category'] for r in conn.execute('SELECT DISTINCT category FROM domains ORDER BY category')]
        conn.close()
        return jsonify({'domains': [_domain_row(r) for r in rows], 'categories': categories, 'count': len(rows)})

    @pub_bp.route('/api/domains/<int:domain_id>', methods=['GET'])
    def get_domain(domain_id):
        conn = get_db()
        r = conn.execute('SELECT * FROM domains WHERE id=?', (domain_id,)).fetchone()
        if not r:
            conn.close()
            return jsonify({'error': 'Domain not found'}), 404
        children = conn.execute('SELECT * FROM domains WHERE parent_name=?', (r['name'],)).fetchall()
        conn.close()
        out = _domain_row(r)
        out['subdomains'] = [_domain_row(c) for c in children]
        return jsonify(out)

    @pub_bp.route('/api/domains/<int:domain_id>/subdomains', methods=['GET'])
    def get_subdomains(domain_id):
        conn = get_db()
        r = conn.execute('SELECT * FROM domains WHERE id=?', (domain_id,)).fetchone()
        if not r:
            conn.close()
            return jsonify({'error': 'Domain not found'}), 404
        children = conn.execute('SELECT * FROM domains WHERE parent_name=?', (r['name'],)).fetchall()
        conn.close()
        return jsonify({'subdomains': [_domain_row(c) for c in children]})

    @pub_bp.route('/api/domains/<int:domain_id>/societies', methods=['GET'])
    def get_domain_societies(domain_id):
        conn = get_db()
        rows = conn.execute('''
            SELECT s.* FROM societies s
            JOIN domain_societies ds ON ds.society_id = s.id
            WHERE ds.domain_id = ? ORDER BY s.name''', (domain_id,)).fetchall()
        conn.close()
        return jsonify({'societies': [_society_row(r) for r in rows]})

    @pub_bp.route('/api/domains/<int:domain_id>/publishers', methods=['GET'])
    def get_domain_publishers(domain_id):
        conn = get_db()
        rows = conn.execute('''
            SELECT p.* FROM publishers p
            JOIN domain_publishers dp ON dp.publisher_id = p.id
            WHERE dp.domain_id = ? ORDER BY p.name''', (domain_id,)).fetchall()
        conn.close()
        return jsonify({'publishers': [_publisher_row(r) for r in rows]})

    def _domain_venues(domain_id, venue_type=None):
        conn = get_db()
        sql = '''SELECT v.* FROM venues v
                 JOIN venue_domains vd ON vd.venue_id = v.id
                 WHERE vd.domain_id = ?'''
        params = [domain_id]
        if venue_type:
            sql += ' AND v.venue_type = ?'
            params.append(venue_type)
        sql += ' ORDER BY v.name'
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [_venue_row(r) for r in rows]

    @pub_bp.route('/api/domains/<int:domain_id>/journals', methods=['GET'])
    def get_domain_journals(domain_id):
        return jsonify({'journals': _domain_venues(domain_id, 'journal')})

    @pub_bp.route('/api/domains/<int:domain_id>/conferences', methods=['GET'])
    def get_domain_conferences(domain_id):
        return jsonify({'conferences': _domain_venues(domain_id, 'conference')})

    @pub_bp.route('/api/journals', methods=['GET'])
    def list_journals():
        conn = get_db()
        rows = conn.execute("SELECT * FROM venues WHERE venue_type='journal' ORDER BY name").fetchall()
        conn.close()
        return jsonify({'journals': [_venue_row(r) for r in rows]})

    @pub_bp.route('/api/journals/<int:venue_id>', methods=['GET'])
    def get_journal(venue_id):
        conn = get_db()
        r = conn.execute('SELECT * FROM venues WHERE id=?', (venue_id,)).fetchone()
        conn.close()
        if not r:
            return jsonify({'error': 'Journal not found'}), 404
        return jsonify(_venue_row(r))

    @pub_bp.route('/api/conferences', methods=['GET'])
    def list_conferences():
        conn = get_db()
        rows = conn.execute("SELECT * FROM venues WHERE venue_type='conference' ORDER BY name").fetchall()
        conn.close()
        return jsonify({'conferences': [_venue_row(r) for r in rows]})

    @pub_bp.route('/api/societies', methods=['GET'])
    def list_societies():
        conn = get_db()
        rows = conn.execute('SELECT * FROM societies ORDER BY name').fetchall()
        conn.close()
        return jsonify({'societies': [_society_row(r) for r in rows]})

    @pub_bp.route('/api/publishers', methods=['GET'])
    def list_publishers():
        conn = get_db()
        rows = conn.execute('SELECT * FROM publishers ORDER BY name').fetchall()
        conn.close()
        return jsonify({'publishers': [_publisher_row(r) for r in rows]})

    @pub_bp.route('/api/classify-research-topic', methods=['POST'])
    @require_auth
    def classify_research_topic():
        data = request.get_json(silent=True) or {}
        query_text = (data.get('query') or data.get('topic') or '').strip()
        if not query_text:
            return jsonify({'error': 'query is required'}), 400
        conn = get_db()
        result = classify_query(conn, query_text)
        import datetime as _dt
        conn.execute(
            '''INSERT INTO query_domain_classifications
               (analysis_id, owner_uid, query_text, primary_domain, primary_confidence,
                secondary_domains_json, low_confidence, created_at)
               VALUES (?,?,?,?,?,?,?,?)''',
            (data.get('analysis_id'), g.uid, query_text,
             (result['primary_domain'] or {}).get('name'),
             (result['primary_domain'] or {}).get('confidence'),
             __import__('json').dumps(result['secondary_domains']),
             1 if result['low_confidence'] else 0,
             _dt.datetime.now().isoformat())
        )
        conn.commit()
        conn.close()
        return jsonify({'query': query_text, **result})

    @pub_bp.route('/api/primary-domains', methods=['GET'])
    def list_primary_domains():
        """Flat 120-item Primary Domain list for the Start Analysis form's
        cascade (Primary -> Secondary -> Research Topic). Separate from
        /api/domains, which powers the categorized Publication Explorer."""
        return jsonify({'primary_domains': PRIMARY_DOMAINS})

    @pub_bp.route('/api/primary-domains/secondary', methods=['GET'])
    def get_secondary_domains():
        primary = request.args.get('primary', '').strip()
        if not primary:
            return jsonify({'error': 'primary is required'}), 400
        secondary = PRIMARY_TO_SECONDARY.get(primary)
        return jsonify({
            'primary': primary,
            'secondary_domains': secondary or [],
            'curated': secondary is not None,
        })

    @pub_bp.route('/api/primary-domains/sources', methods=['GET'])
    def get_primary_domain_sources():
        """Real, queryable paper sources recommended for a Primary Domain
        (see PRIMARY_DOMAIN_SOURCES in publication_data.py). Also reports
        which of the user's originally-requested databases have no public
        API at all (`not_available`) or aren't wired in yet
        (`not_yet_wired`) — never silently drops them without saying so."""
        primary = request.args.get('primary', '').strip()
        if not primary:
            return jsonify({'error': 'primary is required'}), 400
        entry = PRIMARY_DOMAIN_SOURCES.get(primary)
        from academic_sources import keyed_source_status
        return jsonify({
            'primary': primary,
            'curated': entry is not None,
            'sources': (entry or {}).get('sources', []),
            'not_available': (entry or {}).get('not_available', []),
            'not_yet_wired': (entry or {}).get('not_yet_wired', []),
            'keyed_source_status': keyed_source_status(),
        })

    @pub_bp.route('/api/paper-sources/status', methods=['GET'])
    def get_paper_source_status():
        """Which of the 4 key-requiring sources (IEEE Xplore, Springer
        Nature, NASA ADS, CORE) actually have their API key configured in
        this deployment right now — lets the UI show a lock badge instead
        of a checkbox that would silently return nothing."""
        from academic_sources import keyed_source_status
        return jsonify({'keyed_source_status': keyed_source_status()})

    @pub_bp.route('/api/specific-venues', methods=['GET'])
    def get_specific_venues():
        """Real, specific, filterable venue names (see
        SPECIFIC_VENUES_BY_FIELD) for one or more selected fields — pass
        `field` once or several times, e.g.
        ?field=Computer%20Science&field=Computer%20Vision. Only genuine
        venue/journal names are ever returned here, never publisher or
        society umbrella names (those aren't real Semantic Scholar venue
        values — see the comment in publication_data.py)."""
        fields = request.args.getlist('field')
        venues = []
        seen = set()
        for f in fields:
            for v in SPECIFIC_VENUES_BY_FIELD.get(f.strip(), []):
                if v not in seen:
                    seen.add(v)
                    venues.append(v)
        return jsonify({'fields': fields, 'venues': venues})

    app.register_blueprint(pub_bp)
