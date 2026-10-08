"""
Source provenance (architecture-review items 1 and 2): every paper must
be traceable back to exactly which authorized connector fetched it,
when, and — once fields start merging across sources during
deduplication — which source each individual field's current value
actually came from.

`check_source_authorized()` in paper_verification.py already enforces
the allowlist itself (AUTHORIZED_SOURCES — a paper whose `source` isn't
in that set is rejected before any scoring runs, and nothing in this
codebase ever fetches an arbitrary user-supplied URL as a paper source;
academic_sources.py calls each source's own official API directly).
This module is the paper trail that makes that claim auditable per
paper rather than only true in the abstract:

    stamp_provenance(paper, source_name)
        called once, right when a paper dict is first read out of a
        fetch function in deep_collection.collect_source_papers,
        BEFORE dedup/merge. Sets:
          retrieved_at     ISO-8601 UTC timestamp of the real API call
          source_id        that source's own native ID for the record
          source_url       a real, constructed URL to the record on
                            that source's own site (never fabricated —
                            None when no reliable pattern exists)
          identifier_type / identifier
                            the strongest persistent identifier present,
                            by priority DOI > arXiv > PMID > DBLP key >
                            OpenAlex ID > S2 ID > CORE ID > INSPIRE ID
                            > ISBN > none
          field_sources     {field: source_name} for every core field
                            present on THIS record at fetch time

    merge_field_sources(existing, incoming, merged_fields)
        called from PaperDeduper.add_or_merge for exactly the fields
        that were actually copied from `incoming` into `existing`
        (i.e. `existing` didn't have them yet), so field_sources always
        reflects which source's value the merged record is actually
        holding — not every source that ever mentioned the paper.
"""
import datetime

# Priority order matches paper_verification._paper_identifiers, so the
# "strongest identifier" claim here is the same claim verification
# actually checks, not a separate opinion about which ID matters most.
_IDENTIFIER_PRIORITY = (
    ('doi', 'doi'), ('arxiv_id', 'arxiv_id'), ('pmid', 'pmid'),
    ('dblp_key', 'dblp_key'), ('openalex_id', 'openalex_id'),
    ('ss_id', 'ss_id'), ('core_id', 'core_id'),
    ('inspire_id', 'inspire_id'), ('isbn', 'isbn'),
)

# Real, documented URL patterns only. A source with no reliable
# per-record URL pattern (e.g. ERIC, DOAJ search results that don't
# expose a stable per-record permalink in the API response) is left as
# None rather than guessed — a wrong URL is worse than an absent one.
_URL_BUILDERS = {
    'doi': lambda v: f'https://doi.org/{v}',
    'arxiv_id': lambda v: f'https://arxiv.org/abs/{v}',
    'pmid': lambda v: f'https://pubmed.ncbi.nlm.nih.gov/{v}/',
    'dblp_key': lambda v: f'https://dblp.org/rec/{v}.html',
    'openalex_id': lambda v: f'https://openalex.org/{v}',
    'ss_id': lambda v: f'https://www.semanticscholar.org/paper/{v}',
    'core_id': lambda v: f'https://core.ac.uk/works/{v}',
    'inspire_id': lambda v: f'https://inspirehep.net/literature/{v}',
}

# Core bibliographic fields worth attributing to a source. Deliberately
# a small, meaningful set (not every internal field) — provenance on
# "which source said the title was X" is useful; provenance on every
# derived/computed field (impact_score, combined_rank, ...) is not,
# those are computed by THIS pipeline, not fetched from anywhere.
PROVENANCE_FIELDS = ('title', 'authors', 'year', 'venue', 'doi',
                     'abstract', 'citation_count')


def _best_identifier(paper):
    for key, field in _IDENTIFIER_PRIORITY:
        value = paper.get(field)
        if value:
            return key, str(value)
    return None, None


def _source_native_id(paper, source_name, id_type, identifier):
    """The ID this specific source itself would recognize the record
    by — not necessarily the same as the strongest cross-registry
    identifier above (an OpenAlex-fetched record's own ID is its
    openalex_id even if it also carries a DOI)."""
    base = (source_name or '').split(' (via ')[0].strip()
    native_field = {
        'OpenAlex': 'openalex_id', 'Semantic Scholar': 'ss_id',
        'ArXiv': 'arxiv_id', 'PubMed': 'pmid', 'DBLP': 'dblp_key',
        'CORE': 'core_id', 'INSPIRE-HEP': 'inspire_id',
    }.get(base)
    if native_field and paper.get(native_field):
        return str(paper[native_field])
    return identifier  # fall back to the strongest identifier found


def stamp_provenance(paper, source_name):
    """Mutates and returns `paper`. Idempotent-safe to call more than
    once (e.g. if a source's fetch function is retried) — always
    reflects the current record's own fields, never accumulates stale
    data from a previous call."""
    id_type, identifier = _best_identifier(paper)
    paper['retrieved_at'] = datetime.datetime.utcnow().isoformat() + 'Z'
    paper['identifier_type'] = id_type
    paper['identifier'] = identifier
    paper['source_id'] = _source_native_id(paper, source_name, id_type, identifier)

    url = None
    if id_type and id_type in _URL_BUILDERS:
        url = _URL_BUILDERS[id_type](identifier)
    elif paper.get('doi'):
        url = _URL_BUILDERS['doi'](paper['doi'])
    paper['source_url'] = url

    paper['field_sources'] = {
        field: source_name for field in PROVENANCE_FIELDS
        if paper.get(field)
    }
    return paper


def refresh_best_identifier(paper):
    """Recomputes identifier_type/identifier from the paper's CURRENT
    fields. Called after a merge, since a stronger identifier (e.g. a
    DOI from a second source) can arrive after the first stamp — the
    identifier claim should reflect the best evidence now on the
    record, not just what the very first source that indexed it had.
    Deliberately does NOT touch source_id/source_url/retrieved_at,
    which describe the FIRST fetch that produced this record."""
    id_type, identifier = _best_identifier(paper)
    paper['identifier_type'] = id_type
    paper['identifier'] = identifier
    return paper


def merge_field_sources(existing, incoming, merged_fields):
    """Call AFTER copying `merged_fields` from `incoming` into
    `existing` (PaperDeduper.add_or_merge does this for whichever
    _MERGE_FIELDS were empty on `existing`). Updates `field_sources`
    for exactly the fields that changed hands, and ALSO backfills
    provenance-relevant core fields (title/authors/year/venue/doi/
    abstract/citation_count) that `_MERGE_FIELDS` itself doesn't cover
    but which `merged_fields` may still include when the caller passes
    the full set of fields actually copied."""
    existing.setdefault('field_sources', {})
    incoming_sources = incoming.get('field_sources') or {}
    incoming_source_name = incoming.get('source')
    for field in merged_fields:
        if field not in PROVENANCE_FIELDS:
            continue
        # Prefer the incoming record's own recorded attribution (set by
        # stamp_provenance when it was first fetched) over guessing —
        # falls back to its top-level `source` only if that's missing.
        existing['field_sources'][field] = incoming_sources.get(field) or incoming_source_name
    return existing
