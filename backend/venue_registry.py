"""
Venue-level quality and publisher verification (architecture note
sections 7 and 8).

Until now the QUALITY layer scored a venue from whatever happened to be
attached to the PAPER record (a DOAJ flag on the paper's OpenAlex
source stub, an ISSN string, a hard-coded 0.7 for "type == journal").
That is paper-level evidence about a venue, and it is thin.

This module looks the VENUE ITSELF up in a registry and scores it from
that registry's own numbers:

  OpenAlex /sources   real per-venue record keyed on ISSN (exact) or
                      display name (fallback): h-index, 2-year mean
                      citedness, works count, host organization
                      (publisher), source type, is_in_doaj, is_core.
  DOAJ /api/search    independent confirmation that a journal is in the
                      Directory of Open Access Journals, plus the DOAJ
                      Seal where it was awarded.

HONESTY NOTES — these matter more here than anywhere else in the
pipeline, because venue quality is the field where fabricated numbers
are most tempting and hardest for a reader to catch:

 1. THERE IS NO QUARTILE HERE, AND THERE CANNOT BE. Q1/Q2/Q3/Q4 is a
    Scopus (SJR) / Web of Science (JCR) product. Neither has a free
    API, this project has no subscription to either, and no free
    source republishes their quartiles under a license that would let
    us serve them. So `venue_quartile` is ALWAYS None and
    `venue_quartile_basis` always says why. Do not let anyone
    "improve" this by scraping a quartile off a mirror site or
    inferring one from citedness — an inferred quartile presented next
    to real registry numbers is exactly the kind of fabrication the
    rest of this pipeline exists to reject. The architecture note asks
    for quartile as ONE venue signal among several; the others below
    are real and are the ones we use.

 2. `is_core` is OpenAlex's own flag for sources in the CWTS Leiden
    Ranking core collection. It is a real, checkable field from a real
    API — it is NOT a quartile, NOT an impact factor, and NOT a Scopus
    index membership. It is labelled as what it is everywhere it
    surfaces.

 3. A venue that could not be found is recorded as not-found and
    contributes NOTHING to the score (the component is dropped and the
    remaining weights renormalized, as everywhere else in this
    pipeline). A missing venue record is not evidence of a bad venue —
    plenty of legitimate workshops and regional journals are not in
    OpenAlex's source index at all.

 4. Conference venues are NOT penalized. The architecture note is
    explicit about this and it is correct: NeurIPS/CVPR/ACL outrank
    most journals in CS. The scoring below is driven by the venue's
    real citedness and h-index, which is exactly how a strong
    conference earns a strong score.
"""
import os
import re
import math
import time
import requests
from concurrent.futures import ThreadPoolExecutor

TIMEOUT = 15
UA = {'User-Agent': 'ResearchLensAI/1.0 (mailto:megham112004@gmail.com)'}
CROSSREF_MAILTO = os.environ.get('CROSSREF_MAILTO', 'megham112004@gmail.com')

# OpenAlex documents up to 50 OR-ed values in one filter.
OPENALEX_SOURCE_BATCH = 50
# DOAJ is a small volunteer-run service with a published rate limit;
# it is queried only for venues OpenAlex could not resolve, and only
# for this many of them per run.
DOAJ_MAX_LOOKUPS = int(os.environ.get('DEEP_DOAJ_MAX_LOOKUPS', '40'))
DOAJ_PAUSE = float(os.environ.get('DEEP_DOAJ_PAUSE', '0.35'))
VENUE_MAX_WORKERS = int(os.environ.get('DEEP_VENUE_MAX_WORKERS', '4'))

# Weights inside the venue-quality sub-score (0-1). Any component with
# no real data is dropped and the rest renormalized.
VENUE_WEIGHTS = {
    'citedness': 0.35,       # OpenAlex summary_stats.2yr_mean_citedness
    'h_index': 0.25,         # OpenAlex summary_stats.h_index
    'core_collection': 0.20,  # OpenAlex is_core (CWTS Leiden core)
    'indexing': 0.10,        # DOAJ membership (OpenAlex is_in_doaj or DOAJ API)
    'venue_type': 0.10,      # a real journal/conference/book-series record exists
}

QUARTILE_BASIS = ('not_verified — Q1-Q4 quartiles come from Scopus (SJR) and '
                  'Web of Science (JCR), neither of which exposes a free API or '
                  'a redistributable dataset. No quartile is inferred or guessed.')

_cache = {}


def _norm_issn(value):
    if not value:
        return None
    s = re.sub(r'[^0-9Xx]', '', str(value)).upper()
    if len(s) != 8:
        return None
    return f'{s[:4]}-{s[4:]}'


def _norm_name(value):
    return re.sub(r'[^a-z0-9]+', ' ', str(value or '').lower()).strip()


def paper_issns(paper):
    """Every ISSN this paper carries, normalized. `verified_issn` is set
    by paper_verification.compute_authenticity from the Crossref /
    OpenAlex record, so these are registry ISSNs, not strings the
    source's own search result happened to include."""
    raw = paper.get('verified_issn') or []
    if isinstance(raw, str):
        raw = [raw]
    out = []
    for value in raw:
        norm = _norm_issn(value)
        if norm and norm not in out:
            out.append(norm)
    return out


# ─── OpenAlex /sources ──────────────────────────────────────────────

def _source_record(item):
    """Maps one OpenAlex source object to our own shape. Every field is
    copied straight from the API response or left None — nothing here
    is derived, defaulted or filled in."""
    stats = item.get('summary_stats') or {}
    return {
        'found': True,
        'openalex_source_id': (item.get('id') or '').replace('https://openalex.org/', '') or None,
        'display_name': item.get('display_name'),
        'issn_l': _norm_issn(item.get('issn_l')),
        'issn': [i for i in (_norm_issn(v) for v in (item.get('issn') or [])) if i],
        'publisher': item.get('host_organization_name'),
        'host_organization_id': (item.get('host_organization') or '').replace(
            'https://openalex.org/', '') or None,
        'type': (item.get('type') or '').lower() or None,
        'is_in_doaj': item.get('is_in_doaj'),
        # OpenAlex's own CWTS Leiden Ranking core-collection flag. NOT a
        # quartile and not a Scopus index — see honesty note 2.
        'is_core': item.get('is_core'),
        'is_oa': item.get('is_oa'),
        'works_count': item.get('works_count'),
        'cited_by_count': item.get('cited_by_count'),
        'h_index': stats.get('h_index'),
        'i10_index': stats.get('i10_index'),
        'two_yr_mean_citedness': stats.get('2yr_mean_citedness'),
        'country_code': item.get('country_code'),
        'lookup_basis': None,       # set by the caller (issn / display_name)
        'checked_at': time.time(),
    }


def openalex_sources_by_issn(issns):
    """Batched exact ISSN lookup against OpenAlex's free /sources index.
    Returns {issn: record} for every ISSN the record carried, so a
    journal's print and electronic ISSN both resolve to the same
    record. Never raises — a network failure yields no entries, which
    downstream is treated as 'not checked', not as 'bad venue'."""
    out = {}
    wanted = [i for i in dict.fromkeys(issns) if i]
    for start in range(0, len(wanted), OPENALEX_SOURCE_BATCH):
        chunk = wanted[start:start + OPENALEX_SOURCE_BATCH]
        try:
            response = requests.get(
                'https://api.openalex.org/sources',
                params={'filter': 'issn:' + '|'.join(chunk),
                        'per-page': OPENALEX_SOURCE_BATCH,
                        'mailto': CROSSREF_MAILTO},
                headers=UA, timeout=TIMEOUT)
            if response.status_code != 200:
                continue
            for item in (response.json().get('results') or []):
                record = _source_record(item)
                record['lookup_basis'] = 'openalex_issn_exact'
                for issn in filter(None, [record['issn_l']] + record['issn']):
                    if issn in chunk or issn not in out:
                        out[issn] = record
        except Exception:
            continue
    return out


def openalex_source_by_name(venue_name):
    """Fallback for venues with no ISSN in their metadata — most CS
    conferences. Uses OpenAlex's own display-name search and accepts
    the hit ONLY on an exact normalized name match, so 'ICML' does not
    silently resolve to 'ICMLA'."""
    name = (venue_name or '').strip()
    if len(name) < 4:
        return {'found': False, 'reason': 'venue name too short to match safely'}
    try:
        response = requests.get(
            'https://api.openalex.org/sources',
            params={'search': name, 'per-page': 10, 'mailto': CROSSREF_MAILTO},
            headers=UA, timeout=TIMEOUT)
        if response.status_code != 200:
            return {'found': False, 'reason': f'OpenAlex HTTP {response.status_code}'}
        target = _norm_name(name)
        for item in (response.json().get('results') or []):
            candidates = [item.get('display_name')] + list(item.get('alternate_titles') or [])
            if any(_norm_name(c) == target for c in candidates if c):
                record = _source_record(item)
                record['lookup_basis'] = 'openalex_display_name_exact'
                return record
        return {'found': False, 'reason': 'no exact display-name match in OpenAlex /sources'}
    except Exception as e:
        return {'found': False, 'reason': f'OpenAlex source search failed: {e}'}


# ─── DOAJ ───────────────────────────────────────────────────────────

def doaj_lookup(issn):
    """Independent open-access indexing check against DOAJ's free API.
    Returns in_doaj / seal / publisher, or found=False. Used as a
    second, non-OpenAlex indexing witness."""
    issn = _norm_issn(issn)
    if not issn:
        return {'found': False, 'reason': 'no usable ISSN'}
    try:
        response = requests.get(
            f'https://doaj.org/api/search/journals/issn%3A{issn}',
            params={'pageSize': 1}, headers=UA, timeout=TIMEOUT)
        if response.status_code != 200:
            return {'found': False, 'reason': f'DOAJ HTTP {response.status_code}'}
        results = (response.json() or {}).get('results') or []
        if not results:
            return {'found': False, 'reason': 'not listed in DOAJ'}
        bib = results[0].get('bibjson') or {}
        return {
            'found': True,
            'in_doaj': True,
            'seal': bool(results[0].get('admin', {}).get('seal') or bib.get('boai')),
            'title': bib.get('title'),
            'publisher': (bib.get('publisher') or {}).get('name'),
        }
    except Exception as e:
        return {'found': False, 'reason': f'DOAJ lookup failed: {e}'}


# ─── Scoring ────────────────────────────────────────────────────────

def score_venue(record, doaj=None):
    """Turns a venue record into a 0-1 quality value plus a transparent
    per-component breakdown. Components with no data are dropped and
    the remaining weights renormalized — never zero-filled."""
    if not record or not record.get('found'):
        return None, {}, 0.0

    raw = {}
    citedness = record.get('two_yr_mean_citedness')
    if isinstance(citedness, (int, float)) and citedness >= 0:
        # log1p(x)/log1p(20): a 2-year mean citedness of ~20 is the top
        # of the observed range for the strongest venues in OpenAlex.
        raw['citedness'] = min(1.0, math.log1p(citedness) / math.log1p(20))

    h_index = record.get('h_index')
    if isinstance(h_index, int) and h_index >= 0:
        raw['h_index'] = min(1.0, math.log1p(h_index) / math.log1p(400))

    if record.get('is_core') is not None:
        raw['core_collection'] = 1.0 if record.get('is_core') else 0.0

    in_doaj = record.get('is_in_doaj')
    if (doaj or {}).get('found'):
        in_doaj = True
    if in_doaj is not None:
        # DOAJ membership is a positive indexing signal for open-access
        # journals; absence is NOT a negative signal (most subscription
        # journals and every conference are legitimately not in DOAJ),
        # so a False only ever scores neutral-low, never 0.
        raw['indexing'] = 1.0 if in_doaj else 0.45
        if (doaj or {}).get('seal'):
            raw['indexing'] = 1.0

    vtype = record.get('type')
    if vtype:
        raw['venue_type'] = {
            'journal': 1.0, 'conference': 1.0, 'book series': 0.8,
            'ebook platform': 0.5, 'repository': 0.4, 'metadata': 0.3,
        }.get(vtype, 0.5)

    available = sum(VENUE_WEIGHTS[c] for c in raw)
    if available <= 0:
        return None, {}, 0.0
    score, components = 0.0, {}
    for component, value in raw.items():
        weight = VENUE_WEIGHTS[component] / available
        score += value * weight
        components[component] = {'value': round(value, 4),
                                 'weight_pct': round(weight * 100, 1)}
    return round(score, 4), components, available


def annotate_paper(paper, record, doaj=None):
    """Writes the venue findings onto the paper. Every field is either a
    real registry value or None — and `venue_quartile` is always None
    (honesty note 1)."""
    paper['venue_record'] = record if (record and record.get('found')) else None
    paper['venue_lookup_basis'] = (record or {}).get('lookup_basis')
    paper['venue_lookup_reason'] = None if (record or {}).get('found') else (record or {}).get('reason')
    paper['venue_quartile'] = None
    paper['venue_quartile_basis'] = QUARTILE_BASIS

    if not record or not record.get('found'):
        paper['venue_quality_registry'] = None
        paper['venue_quality_components'] = {}
        return paper

    score, components, available = score_venue(record, doaj=doaj)
    paper['venue_quality_registry'] = score
    paper['venue_quality_components'] = components
    paper['venue_quality_available_weight_pct'] = round(available * 100, 1)
    paper['venue_h_index'] = record.get('h_index')
    paper['venue_two_yr_mean_citedness'] = record.get('two_yr_mean_citedness')
    paper['venue_works_count'] = record.get('works_count')
    paper['venue_is_core'] = record.get('is_core')
    paper['venue_is_core_basis'] = ('OpenAlex is_core — CWTS Leiden Ranking core '
                                    'collection membership, not a Scopus/JCR quartile')
    paper['venue_in_doaj'] = record.get('is_in_doaj') or bool((doaj or {}).get('found'))
    paper['venue_doaj_seal'] = (doaj or {}).get('seal')
    paper['venue_type_registry'] = record.get('type')

    # --- publisher verification (architecture note section 8).
    # `verified_publisher` came from the PAPER's Crossref/OpenAlex
    # record; this is the VENUE's registered host organization. Where
    # both exist we say whether they agree, rather than trusting either
    # string on its own.
    registry_publisher = record.get('publisher') or (doaj or {}).get('publisher')
    paper_publisher = paper.get('verified_publisher')
    paper['venue_publisher'] = registry_publisher
    if registry_publisher and paper_publisher:
        a, b = _norm_name(registry_publisher), _norm_name(paper_publisher)
        agree = a == b or a in b or b in a
        paper['publisher_agreement'] = 'agree' if agree else 'disagree'
        paper['publisher_agreement_detail'] = (
            f"venue registry says '{registry_publisher}', paper record says '{paper_publisher}'")
    elif registry_publisher and not paper_publisher:
        paper['verified_publisher'] = registry_publisher
        paper['publisher_verified_by'] = paper.get('publisher_verified_by') or 'OpenAlex /sources'
        paper['publisher_agreement'] = 'venue_registry_only'
    else:
        paper['publisher_agreement'] = 'not_checked'
    return paper


# ─── Entry point ────────────────────────────────────────────────────

def enrich_venue_quality(papers, progress_cb=None, max_name_lookups=None):
    """
    Resolves every distinct venue in `papers` against OpenAlex /sources
    (batched by ISSN, then by exact display name for the ISSN-less
    ones), optionally confirms open-access indexing against DOAJ, and
    annotates each paper.

    Returns a summary dict for the run record. Papers whose venue could
    not be resolved are annotated as not-found and keep scoring
    normally on their other quality components.
    """
    papers = list(papers or [])
    if not papers:
        return {'venues_checked': 0, 'venues_resolved': 0}

    # --- pass 1: ISSN (exact, batched, cheap)
    issn_index = {}
    for paper in papers:
        for issn in paper_issns(paper):
            issn_index.setdefault(issn, []).append(paper)
    if progress_cb and issn_index:
        progress_cb(f'Looking up {len(issn_index)} venue ISSN(s) in OpenAlex /sources...')
    by_issn = openalex_sources_by_issn(list(issn_index)) if issn_index else {}

    resolved_for = {}
    for issn, record in by_issn.items():
        for paper in issn_index.get(issn, []):
            resolved_for.setdefault(id(paper), record)

    # --- pass 2: exact display name, for papers still unresolved.
    unresolved = [p for p in papers
                  if id(p) not in resolved_for and (p.get('venue') or '').strip()]
    # One request per DISTINCT venue name, not per paper.
    by_name_targets, name_index = [], {}
    for paper in unresolved:
        key = _norm_name(paper.get('venue'))
        name_index.setdefault(key, []).append(paper)
    by_name_targets = [papers_[0].get('venue') for papers_ in name_index.values()]
    if max_name_lookups is not None:
        by_name_targets = by_name_targets[:max_name_lookups]
    by_name = {}
    if by_name_targets:
        if progress_cb:
            progress_cb(f'Resolving {len(by_name_targets)} venue name(s) in OpenAlex /sources...')
        with ThreadPoolExecutor(max_workers=max(1, VENUE_MAX_WORKERS)) as pool:
            for name, record in zip(by_name_targets,
                                    pool.map(openalex_source_by_name, by_name_targets)):
                by_name[_norm_name(name)] = record
    for key, record in by_name.items():
        if record and record.get('found'):
            for paper in name_index.get(key, []):
                resolved_for.setdefault(id(paper), record)

    # --- pass 3: DOAJ, only for resolved journal venues OpenAlex did
    # NOT already mark as in-DOAJ (so this is a genuine second witness,
    # not a duplicate of a flag we already have), bounded per run.
    doaj_targets = []
    seen_issn = set()
    for paper in papers:
        record = resolved_for.get(id(paper))
        if not record or record.get('type') != 'journal' or record.get('is_in_doaj'):
            continue
        issn = record.get('issn_l') or (record.get('issn') or [None])[0]
        if issn and issn not in seen_issn:
            seen_issn.add(issn)
            doaj_targets.append(issn)
    doaj_targets = doaj_targets[:DOAJ_MAX_LOOKUPS]
    doaj_by_issn = {}
    if doaj_targets:
        if progress_cb:
            progress_cb(f'Confirming open-access indexing for {len(doaj_targets)} journal(s) in DOAJ...')
        for issn in doaj_targets:
            doaj_by_issn[issn] = doaj_lookup(issn)
            time.sleep(DOAJ_PAUSE)   # DOAJ is small and rate-limited

    for paper in papers:
        record = resolved_for.get(id(paper))
        if record is None:
            venue = (paper.get('venue') or '').strip()
            record = {'found': False,
                      'reason': ('no venue name on this record' if not venue
                                 else 'venue not found in OpenAlex /sources by ISSN or exact name')}
        issn = record.get('issn_l') or (record.get('issn') or [None])[0]
        annotate_paper(paper, record, doaj=doaj_by_issn.get(issn))

    resolved = sum(1 for p in papers if p.get('venue_record'))
    return {
        'venues_checked': len(issn_index) + len(by_name_targets),
        'venues_resolved_by_issn': len(by_issn),
        'venues_resolved_by_name': sum(1 for r in by_name.values() if r and r.get('found')),
        'papers_with_venue_record': resolved,
        'papers_without_venue_record': len(papers) - resolved,
        'doaj_checked': len(doaj_by_issn),
        'doaj_confirmed': sum(1 for r in doaj_by_issn.values() if r and r.get('found')),
        'publisher_agreement_counts': {
            k: sum(1 for p in papers if p.get('publisher_agreement') == k)
            for k in ('agree', 'disagree', 'venue_registry_only', 'not_checked')
        },
        'quartile_basis': QUARTILE_BASIS,
    }
