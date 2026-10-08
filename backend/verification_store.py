"""
Persistence for the three-level verification layer.

Without this module the four scores existed only in one HTTP response
and vanished on refresh — computing them is not the same as being able
to answer "show me every paper we rejected for a retraction, across all
runs", which is the question that makes the layer defensible.

Two tables:

  verification_runs   One row per /api/deep-research run: the topic, the
                      thresholds that were in force, the registry hit
                      counts, and the summary verdict tallies. The
                      thresholds are stored WITH the run because a score
                      is meaningless without them — re-tuning
                      DEEP_RELEVANCE_MIN later must not silently
                      reinterpret last month's results.

  verified_papers     One row per paper per run, kept AND rejected.
                      The four scores are real indexed columns, not
                      fields inside a JSON blob, so they can be sorted,
                      filtered and aggregated in SQL. The full
                      per-component breakdowns are stored alongside as
                      JSON, because those are for reading, not querying.

Rejected papers are stored deliberately. "Here are the 43 records the
system threw out and exactly why" is the evidence that the filter does
something; keeping only the survivors would make the rejection logic
unauditable after the fact.
"""
import json
import re
import datetime

from venue_registry import QUARTILE_BASIS


def init_verification_tables(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS verification_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        topic TEXT NOT NULL,
        owner_uid TEXT,
        queries_used_json TEXT,
        sources_selected_json TEXT,
        -- Thresholds in force for THIS run. Stored per-run so a later
        -- config change can never retroactively reinterpret these rows.
        authenticity_min REAL,
        relevance_min REAL,
        uncorroborated_ceiling REAL,
        papers_total INTEGER DEFAULT 0,
        papers_kept INTEGER DEFAULT 0,
        papers_rejected INTEGER DEFAULT 0,
        retracted_count INTEGER DEFAULT 0,
        crossref_checked INTEGER DEFAULT 0,
        crossref_resolved INTEGER DEFAULT 0,
        crossref_unresolved INTEGER DEFAULT 0,
        crossref_errors INTEGER DEFAULT 0,
        openalex_matched INTEGER DEFAULT 0,
        semantic_scholar_matched INTEGER DEFAULT 0,
        dblp_checked INTEGER DEFAULT 0,
        dblp_resolved INTEGER DEFAULT 0,
        dblp_unresolved INTEGER DEFAULT 0,
        bibliographic_search_attempted INTEGER DEFAULT 0,
        bibliographic_search_matched INTEGER DEFAULT 0,
        pmid_checked INTEGER DEFAULT 0,
        pmid_resolved INTEGER DEFAULT 0,
        other_identifier_checked INTEGER DEFAULT 0,
        other_identifier_resolved INTEGER DEFAULT 0,
        papers_without_doi INTEGER DEFAULT 0,
        network_verified_count INTEGER DEFAULT 0,
        status_counts_json TEXT,
        publication_type_counts_json TEXT,
        venue_verification_json TEXT,
        author_verification_json TEXT,
        created_at TEXT NOT NULL
    )''')

    conn.execute('''CREATE TABLE IF NOT EXISTS verified_papers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL,
        owner_uid TEXT,
        title TEXT NOT NULL,
        doi TEXT,
        arxiv_id TEXT,
        pmid TEXT,
        openalex_id TEXT,
        source TEXT,
        also_indexed_by_json TEXT,
        year TEXT,
        venue TEXT,
        verified_publisher TEXT,
        publisher_verified_by TEXT,
        publication_type TEXT,
        publication_type_source TEXT,
        peer_reviewed TEXT,
        is_retracted INTEGER,              -- 1 / 0 / NULL (NULL = not checked)
        retraction_source_json TEXT,
        source_authorized INTEGER,
        -- The four separate scores, as real columns.
        authenticity_score REAL,
        relevance_score REAL,
        quality_score REAL,
        impact_score REAL,
        overall_score REAL,
        -- What overall_score would have been before the gate zeroed it.
        overall_score_blended REAL,
        authenticity_basis TEXT,
        impact_score_basis TEXT,
        metadata_completeness REAL,
        verification_status TEXT,
        passed_verification INTEGER,
        verified_rank INTEGER,
        combined_rank INTEGER,
        citation_count INTEGER,
        influential_citation_count INTEGER,
        -- Breakdowns: for reading, not for querying.
        verification_reasons_json TEXT,
        authenticity_checks_json TEXT,
        relevance_components_json TEXT,
        relevance_similarity_method TEXT,
        quality_components_json TEXT,
        impact_components_json TEXT,
        corroborating_registries_json TEXT,
        metadata_missing_json TEXT,
        -- Venue-level verification (venue_registry.py). venue_quartile
        -- is deliberately absent: Q1-Q4 is a Scopus/JCR product with no
        -- free API, so no column here could ever hold a real one and a
        -- column that only ever holds a guess is worse than no column.
        venue_quality_registry REAL,
        venue_lookup_basis TEXT,
        venue_h_index INTEGER,
        venue_two_yr_mean_citedness REAL,
        venue_is_core INTEGER,
        venue_in_doaj INTEGER,
        venue_publisher TEXT,
        publisher_agreement TEXT,
        venue_quality_components_json TEXT,
        -- Author-level verification (author_verification.py, ORCID).
        author_orcid_status TEXT,
        author_orcid_works_count INTEGER,
        author_verification_value REAL,
        author_orcid_checks_json TEXT,
        author_verified_affiliations_json TEXT,
        -- Source provenance (provenance.py). retrieved_at/source_id/
        -- source_url describe the FIRST fetch that produced this
        -- record; identifier_type/identifier reflect the BEST
        -- identifier known after cross-source merging (may be
        -- stronger than what the first source alone provided).
        retrieved_at TEXT,
        source_id TEXT,
        source_url TEXT,
        identifier_type TEXT,
        identifier TEXT,
        field_sources_json TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(run_id) REFERENCES verification_runs(id)
    )''')

    # --- migration: add the venue/author verification columns to
    # databases created by an earlier version. SQLite has no
    # ADD COLUMN IF NOT EXISTS, so existing columns are read first and
    # only the genuinely missing ones are added. No data is rewritten:
    # rows from older runs keep NULL, which reads back as "not checked"
    # rather than as a failed check.
    def _add_missing(table, columns):
        existing = {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}
        for name, decl in columns:
            if name not in existing:
                conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {decl}')

    _add_missing('verified_papers', [
        ('venue_quality_registry', 'REAL'), ('venue_lookup_basis', 'TEXT'),
        ('venue_h_index', 'INTEGER'), ('venue_two_yr_mean_citedness', 'REAL'),
        ('venue_is_core', 'INTEGER'), ('venue_in_doaj', 'INTEGER'),
        ('venue_publisher', 'TEXT'), ('publisher_agreement', 'TEXT'),
        ('venue_quality_components_json', 'TEXT'),
        ('author_orcid_status', 'TEXT'), ('author_orcid_works_count', 'INTEGER'),
        ('author_verification_value', 'REAL'), ('author_orcid_checks_json', 'TEXT'),
        ('author_verified_affiliations_json', 'TEXT'),
        ('retrieved_at', 'TEXT'), ('source_id', 'TEXT'), ('source_url', 'TEXT'),
        ('identifier_type', 'TEXT'), ('identifier', 'TEXT'), ('field_sources_json', 'TEXT'),
        ('relevance_similarity_method', 'TEXT'),
    ])
    _add_missing('verification_runs', [
        ('venue_verification_json', 'TEXT'), ('author_verification_json', 'TEXT'),
        # 'deep' = /api/deep-research, 'standard' = /api/analyze[-multi].
        # Older rows read back NULL, which the API reports as 'deep' (the
        # only mode that produced runs before this column existed).
        ('mode', 'TEXT'), ('speed', 'TEXT'),
    ])

    for stmt in (
        'CREATE INDEX IF NOT EXISTS idx_vp_run ON verified_papers(run_id)',
        'CREATE INDEX IF NOT EXISTS idx_vp_status ON verified_papers(verification_status)',
        'CREATE INDEX IF NOT EXISTS idx_vp_overall ON verified_papers(overall_score DESC)',
        'CREATE INDEX IF NOT EXISTS idx_vp_auth ON verified_papers(authenticity_score)',
        'CREATE INDEX IF NOT EXISTS idx_vp_doi ON verified_papers(doi)',
        'CREATE INDEX IF NOT EXISTS idx_vp_owner ON verified_papers(owner_uid)',
        'CREATE INDEX IF NOT EXISTS idx_vr_owner ON verification_runs(owner_uid)',
    ):
        conn.execute(stmt)
    conn.commit()


def _norm_doi(doi):
    """DOIs arrive in several shapes (bare, https://doi.org/..., mixed
    case). They are stored normalized so a later lookup by DOI actually
    matches instead of silently returning nothing."""
    d = (doi or '').strip().lower()
    d = re.sub(r'^https?://(dx\.)?doi\.org/', '', d)
    return d or None


def _json(value):
    return json.dumps(value) if value is not None else None


def _int_or_none(value):
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _bool_col(value):
    """True -> 1, False -> 0, None -> NULL. The three-way distinction
    matters: `is_retracted IS NULL` means "we never checked", which is
    not the same claim as "not retracted"."""
    if value is None:
        return None
    return 1 if value else 0


def save_verification_run(conn, topic, summary, kept, rejected, owner_uid=None,
                          queries_used=None, sources_selected=None,
                          mode='deep', speed=None):
    """
    Writes one run plus every paper (kept and rejected) in a single
    transaction. Returns the new run id.

    `summary` is the dict returned by paper_verification.verify_papers.
    """
    now = datetime.datetime.now().isoformat()
    thresholds = summary.get('thresholds') or {}

    cur = conn.execute(
        '''INSERT INTO verification_runs
           (topic, owner_uid, queries_used_json, sources_selected_json,
            authenticity_min, relevance_min, uncorroborated_ceiling,
            papers_total, papers_kept, papers_rejected, retracted_count,
            crossref_checked, crossref_resolved, crossref_unresolved, crossref_errors,
            openalex_matched, semantic_scholar_matched, dblp_checked, dblp_resolved,
            dblp_unresolved, bibliographic_search_attempted, bibliographic_search_matched,
            pmid_checked, pmid_resolved, other_identifier_checked, other_identifier_resolved,
            papers_without_doi,
            network_verified_count, status_counts_json, publication_type_counts_json,
            venue_verification_json, author_verification_json,
            created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (topic, owner_uid, _json(queries_used), _json(sources_selected),
         thresholds.get('authenticity_min'), thresholds.get('relevance_min'),
         thresholds.get('uncorroborated_ceiling'),
         summary.get('verified', 0), summary.get('kept_count', 0),
         summary.get('rejected_count', 0), summary.get('retracted_count', 0),
         summary.get('crossref_checked', 0), summary.get('crossref_resolved', 0),
         summary.get('crossref_unresolved', 0), summary.get('crossref_errors', 0),
         summary.get('openalex_matched', 0), summary.get('semantic_scholar_matched', 0),
         summary.get('dblp_checked', 0), summary.get('dblp_resolved', 0),
         summary.get('dblp_unresolved', 0),
         summary.get('bibliographic_search_attempted', 0),
         summary.get('bibliographic_search_matched', 0),
         summary.get('pmid_checked', 0), summary.get('pmid_resolved', 0),
         summary.get('other_identifier_checked', 0), summary.get('other_identifier_resolved', 0),
         summary.get('papers_without_doi', 0), summary.get('network_verified_count', 0),
         _json(summary.get('status_counts')), _json(summary.get('publication_type_counts')),
         _json(summary.get('venue_verification')), _json(summary.get('author_verification')),
         now))
    run_id = cur.lastrowid
    conn.execute('UPDATE verification_runs SET mode = ?, speed = ? WHERE id = ?',
                 (mode, speed, run_id))

    rows = []
    for paper in list(kept or []) + list(rejected or []):
        rows.append((
            run_id, owner_uid,
            paper.get('title') or '', _norm_doi(paper.get('doi')),
            paper.get('arxiv_id') or None, paper.get('pmid') or None,
            paper.get('openalex_id') or None, paper.get('source') or None,
            _json(paper.get('also_indexed_by')),
            str(paper.get('year')) if paper.get('year') is not None else None,
            paper.get('venue') or None,
            paper.get('verified_publisher'), paper.get('publisher_verified_by'),
            paper.get('publication_type'), paper.get('publication_type_source'),
            str(paper.get('peer_reviewed')) if paper.get('peer_reviewed') is not None else None,
            _bool_col(paper.get('is_retracted')), _json(paper.get('retraction_source')),
            _bool_col(paper.get('source_authorized')),
            paper.get('authenticity_score'), paper.get('relevance_score'),
            paper.get('quality_score'), paper.get('impact_score'),
            paper.get('overall_score'), paper.get('overall_score_blended'),
            paper.get('authenticity_basis'), paper.get('impact_score_basis'),
            paper.get('metadata_completeness'),
            paper.get('verification_status'), _bool_col(paper.get('passed_verification')),
            _int_or_none(paper.get('verified_rank')), _int_or_none(paper.get('combined_rank')),
            _int_or_none(paper.get('citation_count')),
            _int_or_none(paper.get('influential_citation_count')),
            _json(paper.get('verification_reasons')),
            _json(paper.get('authenticity_checks')),
            _json(paper.get('relevance_components')),
            _json(paper.get('quality_components')),
            _json(paper.get('impact_score_components')),
            _json(paper.get('corroborating_registries')),
            _json(paper.get('metadata_missing')),
            paper.get('venue_quality_registry'), paper.get('venue_lookup_basis'),
            _int_or_none(paper.get('venue_h_index')),
            paper.get('venue_two_yr_mean_citedness'),
            _bool_col(paper.get('venue_is_core')), _bool_col(paper.get('venue_in_doaj')),
            paper.get('venue_publisher'), paper.get('publisher_agreement'),
            _json(paper.get('venue_quality_components')),
            paper.get('author_orcid_status'),
            _int_or_none(paper.get('author_orcid_works_count')),
            paper.get('author_verification_value'),
            _json(paper.get('author_orcid_checks')),
            _json(paper.get('author_verified_affiliations')),
            paper.get('retrieved_at'), paper.get('source_id'), paper.get('source_url'),
            paper.get('identifier_type'), paper.get('identifier'),
            _json(paper.get('field_sources')),
            paper.get('relevance_similarity_method'),
            now,
        ))

    conn.executemany(
        '''INSERT INTO verified_papers
           (run_id, owner_uid, title, doi, arxiv_id, pmid, openalex_id, source,
            also_indexed_by_json, year, venue, verified_publisher, publisher_verified_by,
            publication_type, publication_type_source, peer_reviewed, is_retracted,
            retraction_source_json, source_authorized,
            authenticity_score, relevance_score, quality_score, impact_score, overall_score,
            overall_score_blended, authenticity_basis, impact_score_basis, metadata_completeness,
            verification_status, passed_verification, verified_rank, combined_rank,
            citation_count, influential_citation_count,
            verification_reasons_json, authenticity_checks_json, relevance_components_json,
            quality_components_json, impact_components_json, corroborating_registries_json,
            metadata_missing_json,
            venue_quality_registry, venue_lookup_basis, venue_h_index,
            venue_two_yr_mean_citedness, venue_is_core, venue_in_doaj,
            venue_publisher, publisher_agreement, venue_quality_components_json,
            author_orcid_status, author_orcid_works_count, author_verification_value,
            author_orcid_checks_json, author_verified_affiliations_json,
            retrieved_at, source_id, source_url, identifier_type, identifier, field_sources_json,
            relevance_similarity_method,
            created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                   ?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                   ?,?,?,?,?,?,
                   ?)''',
        rows)
    conn.commit()
    return run_id


# ─── Read side ──────────────────────────────────────────────────────

_ALLOWED_SORT = {
    'overall_score', 'authenticity_score', 'relevance_score', 'quality_score',
    'impact_score', 'verified_rank', 'year', 'citation_count',
}


def _col(row, name):
    """Reads a column that may not exist on rows written by an older
    schema. sqlite3.Row raises IndexError for an unknown key, and a
    missing column must read as "not checked" (None), never as 0/False."""
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


def _tri(value):
    """0/1/NULL -> False/True/None. None stays None: "we did not check"
    and "we checked and it was false" are different findings."""
    return None if value is None else bool(value)


def _loads(value):
    if not value:
        return None
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return None


def run_row_to_dict(row):
    return {
        'id': row['id'], 'topic': row['topic'], 'created_at': row['created_at'],
        'queries_used': _loads(row['queries_used_json']),
        'sources_selected': _loads(row['sources_selected_json']),
        'thresholds': {
            'authenticity_min': row['authenticity_min'],
            'relevance_min': row['relevance_min'],
            'uncorroborated_ceiling': row['uncorroborated_ceiling'],
        },
        'mode': _col(row, 'mode') or 'deep', 'speed': _col(row, 'speed'),
        'papers_total': row['papers_total'], 'papers_kept': row['papers_kept'],
        'papers_rejected': row['papers_rejected'], 'retracted_count': row['retracted_count'],
        'crossref_checked': row['crossref_checked'], 'crossref_resolved': row['crossref_resolved'],
        'crossref_unresolved': row['crossref_unresolved'], 'crossref_errors': row['crossref_errors'],
        'openalex_matched': row['openalex_matched'],
        'semantic_scholar_matched': row['semantic_scholar_matched'],
        'dblp_checked': row['dblp_checked'], 'dblp_resolved': row['dblp_resolved'],
        'dblp_unresolved': row['dblp_unresolved'],
        'bibliographic_search_attempted': row['bibliographic_search_attempted'],
        'bibliographic_search_matched': row['bibliographic_search_matched'],
        'pmid_checked': row['pmid_checked'], 'pmid_resolved': row['pmid_resolved'],
        'other_identifier_checked': row['other_identifier_checked'],
        'other_identifier_resolved': row['other_identifier_resolved'],
        'papers_without_doi': row['papers_without_doi'],
        'network_verified_count': row['network_verified_count'],
        'status_counts': _loads(row['status_counts_json']),
        'publication_type_counts': _loads(row['publication_type_counts_json']),
        # Rows written before these columns existed read back as None,
        # which the UI shows as "not checked" — never as a failed check.
        'venue_verification': _loads(_col(row, 'venue_verification_json')),
        'author_verification': _loads(_col(row, 'author_verification_json')),
    }


def paper_row_to_dict(row, include_breakdowns=True):
    out = {
        'id': row['id'], 'run_id': row['run_id'], 'title': row['title'],
        'doi': row['doi'], 'arxiv_id': row['arxiv_id'], 'pmid': row['pmid'],
        'source': row['source'], 'also_indexed_by': _loads(row['also_indexed_by_json']),
        'year': row['year'], 'venue': row['venue'],
        'verified_publisher': row['verified_publisher'],
        'publisher_verified_by': row['publisher_verified_by'],
        'publication_type': row['publication_type'],
        'publication_type_source': row['publication_type_source'],
        'peer_reviewed': row['peer_reviewed'],
        # Preserved as a three-way value: None means never checked.
        'is_retracted': None if row['is_retracted'] is None else bool(row['is_retracted']),
        'retraction_source': _loads(row['retraction_source_json']),
        'source_authorized': None if row['source_authorized'] is None else bool(row['source_authorized']),
        'authenticity_score': row['authenticity_score'],
        'relevance_score': row['relevance_score'],
        'quality_score': row['quality_score'],
        'impact_score': row['impact_score'],
        'overall_score': row['overall_score'],
        'overall_score_blended': row['overall_score_blended'],
        'authenticity_basis': row['authenticity_basis'],
        'impact_score_basis': row['impact_score_basis'],
        'metadata_completeness': row['metadata_completeness'],
        'verification_status': row['verification_status'],
        'passed_verification': bool(row['passed_verification']),
        'verified_rank': row['verified_rank'], 'combined_rank': row['combined_rank'],
        'citation_count': row['citation_count'],
        'influential_citation_count': row['influential_citation_count'],
        'verification_reasons': _loads(row['verification_reasons_json']),
        # --- venue-level verification. No quartile field exists here by
        # design (see the table definition); `venue_quartile_basis` says
        # so explicitly rather than leaving a reader to wonder.
        'venue_quality_registry': _col(row, 'venue_quality_registry'),
        'venue_lookup_basis': _col(row, 'venue_lookup_basis'),
        'venue_h_index': _col(row, 'venue_h_index'),
        'venue_two_yr_mean_citedness': _col(row, 'venue_two_yr_mean_citedness'),
        'venue_is_core': _tri(_col(row, 'venue_is_core')),
        'venue_in_doaj': _tri(_col(row, 'venue_in_doaj')),
        'venue_publisher': _col(row, 'venue_publisher'),
        'publisher_agreement': _col(row, 'publisher_agreement'),
        'venue_quartile': None,
        'venue_quartile_basis': QUARTILE_BASIS,
        # --- author-level (ORCID) verification
        'author_orcid_status': _col(row, 'author_orcid_status'),
        'author_orcid_works_count': _col(row, 'author_orcid_works_count'),
        'author_verification_value': _col(row, 'author_verification_value'),
        'author_verified_affiliations': _loads(_col(row, 'author_verified_affiliations_json')),
        # --- provenance (architecture-review items 1/2): retrieved_at/
        # source_id/source_url describe the FIRST fetch that produced
        # this record; identifier_type/identifier reflect the best
        # identifier known after cross-source merging.
        'retrieved_at': _col(row, 'retrieved_at'),
        'source_id': _col(row, 'source_id'),
        'source_url': _col(row, 'source_url'),
        'identifier_type': _col(row, 'identifier_type'),
        'identifier': _col(row, 'identifier'),
        'field_sources': _loads(_col(row, 'field_sources_json')),
        'relevance_similarity_method': _col(row, 'relevance_similarity_method'),
    }
    if include_breakdowns:
        out.update({
            'authenticity_checks': _loads(row['authenticity_checks_json']),
            'relevance_components': _loads(row['relevance_components_json']),
            'quality_components': _loads(row['quality_components_json']),
            'impact_score_components': _loads(row['impact_components_json']),
            'corroborating_registries': _loads(row['corroborating_registries_json']),
            'metadata_missing': _loads(row['metadata_missing_json']),
            'venue_quality_components': _loads(_col(row, 'venue_quality_components_json')),
            'author_orcid_checks': _loads(_col(row, 'author_orcid_checks_json')),
        })
    return out


def list_runs(conn, owner_uid, limit=50, offset=0, mode=None):
    where, params = 'owner_uid = ?', [owner_uid]
    if mode == 'deep':
        # rows written before the mode column existed are deep runs
        where += " AND (mode = 'deep' OR mode IS NULL)"
    elif mode:
        where += ' AND mode = ?'
        params.append(mode)
    rows = conn.execute(
        f'''SELECT * FROM verification_runs WHERE {where}
           ORDER BY id DESC LIMIT ? OFFSET ?''',
        params + [min(int(limit), 200), max(0, int(offset))]).fetchall()
    return [run_row_to_dict(r) for r in rows]


def get_run(conn, run_id, owner_uid):
    row = conn.execute('SELECT * FROM verification_runs WHERE id = ? AND owner_uid = ?',
                       (run_id, owner_uid)).fetchone()
    return run_row_to_dict(row) if row else None


def list_run_papers(conn, run_id, owner_uid, status=None, publication_type=None,
                    passed=None, min_authenticity=None, min_relevance=None,
                    min_quality=None, sort='overall_score', order='desc',
                    limit=100, offset=0, include_breakdowns=False):
    """
    Filtered, sorted read over one run's papers. `sort` is validated
    against a whitelist — these values reach an ORDER BY clause, which
    cannot be parameterized, so an unvalidated value here would be SQL
    injection.
    """
    where = ['run_id = ?', 'owner_uid = ?']
    params = [run_id, owner_uid]
    if status:
        where.append('verification_status = ?')
        params.append(status)
    if publication_type:
        where.append('publication_type = ?')
        params.append(publication_type)
    if passed is not None:
        where.append('passed_verification = ?')
        params.append(1 if passed else 0)
    for column, value in (('authenticity_score', min_authenticity),
                          ('relevance_score', min_relevance),
                          ('quality_score', min_quality)):
        if value is not None:
            where.append(f'{column} >= ?')
            params.append(float(value))

    sort_col = sort if sort in _ALLOWED_SORT else 'overall_score'
    direction = 'ASC' if str(order).lower() == 'asc' else 'DESC'
    params.extend([min(int(limit), 500), max(0, int(offset))])

    rows = conn.execute(
        f'''SELECT * FROM verified_papers WHERE {' AND '.join(where)}
            ORDER BY {sort_col} {direction} LIMIT ? OFFSET ?''', params).fetchall()
    return [paper_row_to_dict(r, include_breakdowns) for r in rows]


def run_score_stats(conn, run_id, owner_uid):
    """Cross-source aggregate over one run — the query that only becomes
    possible once the scores are columns rather than a JSON blob."""
    rows = conn.execute(
        '''SELECT source,
                  COUNT(*)                        AS papers,
                  SUM(passed_verification)        AS passed,
                  ROUND(AVG(authenticity_score),2) AS avg_authenticity,
                  ROUND(AVG(relevance_score),2)    AS avg_relevance,
                  ROUND(AVG(quality_score),2)      AS avg_quality,
                  ROUND(AVG(impact_score),2)       AS avg_impact,
                  ROUND(AVG(overall_score),2)      AS avg_overall,
                  -- Averaged over passing papers only: including
                  -- rejected records (whose overall_score is zeroed)
                  -- would make a source look worse than its surviving
                  -- papers actually are.
                  ROUND(AVG(CASE WHEN passed_verification = 1
                                 THEN overall_score END),2) AS avg_overall_passed,
                  SUM(CASE WHEN is_retracted = 1 THEN 1 ELSE 0 END) AS retracted
           FROM verified_papers WHERE run_id = ? AND owner_uid = ?
           GROUP BY source ORDER BY avg_overall_passed IS NULL, avg_overall_passed DESC''',
        (run_id, owner_uid)).fetchall()
    return [dict(r) for r in rows]


def paper_history(conn, doi, owner_uid, limit=20):
    """Every time this DOI has been verified, across runs — so a score
    that changed between runs can be traced instead of guessed at."""
    rows = conn.execute(
        '''SELECT vp.*, vr.topic AS run_topic, vr.created_at AS run_created_at
           FROM verified_papers vp JOIN verification_runs vr ON vr.id = vp.run_id
           WHERE vp.doi = ? AND vp.owner_uid = ?
           ORDER BY vp.id DESC LIMIT ?''',
        (doi, owner_uid, min(int(limit), 100))).fetchall()
    out = []
    for r in rows:
        entry = paper_row_to_dict(r)
        entry['run_topic'] = r['run_topic']
        entry['run_created_at'] = r['run_created_at']
        out.append(entry)
    return out