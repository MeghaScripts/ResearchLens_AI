"""
Author authenticity (architecture note section 13):

    Author -> ORCID -> Affiliation -> Other publications

Until now the pipeline COLLECTED ORCIDs from Crossref/OpenAlex and
treated their mere presence as a 0.8 quality signal. That is weak: an
ORCID string in a metadata record is just a string, and a fabricated
record can carry a fabricated one. This module actually resolves it.

Two checks, in order of cost:

  1. CHECKSUM (offline, free, deterministic). An ORCID iD's final
     character is an ISO 7064 MOD 11-2 check digit over the first 15.
     A made-up 16-digit string fails this essentially always, so a
     fabricated ORCID is caught before any network call happens.

  2. RESOLUTION (ORCID public API, free, no key required).
     pub.orcid.org/v3.0/{id}/record returns the registered person:
     their name, their employment/affiliation history, and how many
     works are attached to the record. We then check that the name on
     the ORCID record actually matches the author name on the paper —
     a real ORCID belonging to a DIFFERENT person is the signature of
     a copied or careless record, and it is invisible if you only
     check that the ORCID resolves.

HONESTY NOTES:

 1. Absence of an ORCID is NOT a problem and is never penalized. Most
    published authors have no ORCID in the metadata, and whole
    disciplines barely use them. A paper with no ORCID simply has this
    component DROPPED from its quality score (and the remaining
    weights renormalized), exactly as elsewhere in this pipeline.

 2. A failed ORCID check is a flag on the METADATA, not an accusation
    against a researcher. `author_orcid_status` distinguishes
    'invalid_checksum' (the string cannot be a real ORCID),
    'unresolved' (well-formed but no such record), 'name_mismatch'
    (resolves to a different person) and 'verified'. Only the first
    two are treated as negative evidence in scoring; a name mismatch
    is reported but weighted mildly, because legitimate name changes,
    transliterations and initials-only author lists all produce it.

 3. Nothing here ever reaches the AUTHENTICITY score or the reject
    gate. Author metadata is a QUALITY signal. A paper is not rejected
    because an ORCID did not resolve.

 4. The ORCID public API is free and unauthenticated for public
    records, but it is a shared community service — lookups are
    deduplicated per ORCID, bounded per run (ORCID_MAX_LOOKUPS) and
    run with a small worker pool.
"""
import os
import re
import difflib
import requests
from concurrent.futures import ThreadPoolExecutor

TIMEOUT = 15
HEADERS = {'Accept': 'application/json',
           'User-Agent': 'ResearchLensAI/1.0 (mailto:megham112004@gmail.com)'}

ORCID_MAX_LOOKUPS = int(os.environ.get('DEEP_ORCID_MAX_LOOKUPS', '150'))
ORCID_MAX_WORKERS = int(os.environ.get('DEEP_ORCID_MAX_WORKERS', '4'))
# How many ORCIDs to actually resolve per paper. Verifying one author
# establishes that the author list points at a real, registered
# researcher; resolving all twelve authors of a physics paper spends
# the run's whole budget on one record.
ORCID_PER_PAPER = int(os.environ.get('DEEP_ORCID_PER_PAPER', '3'))

NAME_MATCH_RATIO = 0.80

_ORCID_RE = re.compile(r'(\d{4}-\d{4}-\d{4}-\d{3}[\dX])', re.I)


def normalize_orcid(value):
    """Extracts a bare 0000-0000-0000-0000 iD from any of the shapes the
    registries hand back (bare, http://orcid.org/..., https, with or
    without a trailing slash). Returns None if there is no well-formed
    iD in the string at all."""
    if not value:
        return None
    match = _ORCID_RE.search(str(value))
    return match.group(1).upper() if match else None


def checksum_valid(orcid):
    """ISO 7064 MOD 11-2, the check-digit algorithm ORCID specifies for
    its iDs. Pure arithmetic — no network, no API, no guessing."""
    orcid = normalize_orcid(orcid)
    if not orcid:
        return False
    digits = orcid.replace('-', '')
    total = 0
    for char in digits[:-1]:
        if not char.isdigit():
            return False
        total = (total + int(char)) * 2
    remainder = total % 11
    expected = (12 - remainder) % 11
    check = 'X' if expected == 10 else str(expected)
    return check == digits[-1]


def _norm_name(value):
    return re.sub(r'[^a-z]+', ' ', str(value or '').lower()).strip()


def _surname(value):
    parts = _norm_name(value).split()
    return parts[-1] if parts else ''


def name_matches(record_name, paper_authors):
    """True when the name on the ORCID record plausibly belongs to
    someone in this paper's author list. Deliberately generous on given
    names (initials, middle names, transliteration) and strict on
    surnames, because that is where the real signal is."""
    record_surname = _surname(record_name)
    if not record_surname:
        return None
    best = 0.0
    for author in (paper_authors or []):
        name = author if isinstance(author, str) else (
            author.get('name') or f"{author.get('given', '')} {author.get('family', '')}")
        if _surname(name) == record_surname:
            return True
        best = max(best, difflib.SequenceMatcher(
            None, _norm_name(name), _norm_name(record_name)).ratio())
    return best >= NAME_MATCH_RATIO


def orcid_lookup(orcid):
    """Resolves one iD against the ORCID public API. Returns the
    registered name, affiliations and work count, or found=False with a
    reason. Never raises."""
    orcid = normalize_orcid(orcid)
    if not orcid:
        return {'found': False, 'reason': 'not a well-formed ORCID iD'}
    if not checksum_valid(orcid):
        # Fabricated iDs are caught here, before any network call.
        return {'found': False, 'orcid': orcid, 'checksum_valid': False,
                'reason': 'ORCID check digit (ISO 7064 MOD 11-2) does not validate'}
    try:
        response = requests.get(f'https://pub.orcid.org/v3.0/{orcid}/record',
                                headers=HEADERS, timeout=TIMEOUT)
        if response.status_code == 404:
            return {'found': False, 'orcid': orcid, 'checksum_valid': True,
                    'reason': 'no such record in the ORCID registry'}
        if response.status_code != 200:
            return {'found': False, 'orcid': orcid, 'checksum_valid': True,
                    'reason': f'ORCID HTTP {response.status_code}', 'error': True}
        data = response.json() or {}

        person = (data.get('person') or {})
        name_block = person.get('name') or {}
        given = ((name_block.get('given-names') or {}) or {}).get('value') or ''
        family = ((name_block.get('family-name') or {}) or {}).get('value') or ''
        credit = ((name_block.get('credit-name') or {}) or {}).get('value') or ''
        display_name = (credit or f'{given} {family}').strip()

        affiliations = []
        activities = data.get('activities-summary') or {}
        for group in ((activities.get('employments') or {}).get('affiliation-group') or []):
            for summary in (group.get('summaries') or []):
                emp = summary.get('employment-summary') or {}
                org = (emp.get('organization') or {}).get('name')
                if org and org not in affiliations:
                    affiliations.append(org)
        if not affiliations:
            for group in ((activities.get('educations') or {}).get('affiliation-group') or []):
                for summary in (group.get('summaries') or []):
                    edu = summary.get('education-summary') or {}
                    org = (edu.get('organization') or {}).get('name')
                    if org and org not in affiliations:
                        affiliations.append(org)

        works_count = len((activities.get('works') or {}).get('group') or [])

        return {
            'found': True,
            'orcid': orcid,
            'checksum_valid': True,
            'name': display_name or None,
            'affiliations': affiliations[:5],
            'works_count': works_count,
            'record_url': f'https://orcid.org/{orcid}',
        }
    except Exception as e:
        return {'found': False, 'orcid': orcid, 'checksum_valid': True,
                'reason': f'ORCID lookup failed: {e}', 'error': True}


def _paper_orcids(paper):
    raw = paper.get('author_orcids') or []
    if isinstance(raw, str):
        raw = [raw]
    out = []
    for value in raw:
        orcid = normalize_orcid(value)
        if orcid and orcid not in out:
            out.append(orcid)
    return out[:ORCID_PER_PAPER]


def annotate_paper(paper, results_by_orcid):
    """Writes per-paper author findings. Sets:
        author_orcid_checks   one entry per iD actually checked
        author_orcid_status   verified / name_mismatch / unresolved /
                              invalid_checksum / not_checked
        author_verified_affiliations, author_orcid_works_count
        author_verification_value   0-1 quality contribution, or None
    """
    orcids = _paper_orcids(paper)
    checks = []
    for orcid in orcids:
        result = results_by_orcid.get(orcid)
        if result is None:
            continue
        matched = name_matches(result.get('name'), paper.get('authors')) if result.get('found') else None
        if not result.get('found'):
            status = ('invalid_checksum' if result.get('checksum_valid') is False
                      else 'error' if result.get('error') else 'unresolved')
        elif matched is False:
            status = 'name_mismatch'
        else:
            status = 'verified'
        checks.append({
            'orcid': orcid,
            'status': status,
            'registered_name': result.get('name'),
            'name_matches_paper': matched,
            'affiliations': result.get('affiliations') or [],
            'works_count': result.get('works_count'),
            'reason': result.get('reason'),
            'record_url': result.get('record_url') or f'https://orcid.org/{orcid}',
        })

    paper['author_orcid_checks'] = checks
    if not checks:
        # No ORCID on the record is the normal case and is not a finding.
        paper['author_orcid_status'] = 'not_checked'
        paper['author_verification_value'] = None
        paper['author_verification_basis'] = 'no ORCID iD present in any registry record for this paper'
        return paper

    statuses = [c['status'] for c in checks]
    if 'verified' in statuses:
        overall = 'verified'
    elif 'name_mismatch' in statuses:
        overall = 'name_mismatch'
    elif 'invalid_checksum' in statuses:
        overall = 'invalid_checksum'
    elif 'unresolved' in statuses:
        overall = 'unresolved'
    else:
        overall = 'not_checked'

    affiliations = [a for c in checks for a in (c.get('affiliations') or [])]
    works = [c['works_count'] for c in checks if isinstance(c.get('works_count'), int)]
    paper['author_orcid_status'] = overall
    paper['author_verified_affiliations'] = list(dict.fromkeys(affiliations))[:5]
    paper['author_orcid_works_count'] = max(works) if works else None

    if overall == 'not_checked':
        paper['author_verification_value'] = None
        paper['author_verification_basis'] = 'ORCID service unavailable for this record'
        return paper

    # Scoring. A verified iD is strong evidence the author list points at
    # a registered researcher; the bonus above it comes from the record's
    # own real contents (affiliation on file, other works), which is the
    # "Affiliation -> Other publications" leg of the note's section 13.
    base = {'verified': 0.80, 'name_mismatch': 0.45,
            'unresolved': 0.20, 'invalid_checksum': 0.0}[overall]
    if overall == 'verified':
        if paper.get('author_verified_affiliations'):
            base += 0.10
        if (paper.get('author_orcid_works_count') or 0) >= 5:
            base += 0.10
    paper['author_verification_value'] = round(min(1.0, base), 4)
    paper['author_verification_basis'] = {
        'verified': 'ORCID iD resolves and the registered name matches this paper\'s author list',
        'name_mismatch': 'ORCID iD resolves but to a differently-named researcher — reported, weighted mildly (name changes and initials-only author lists cause this legitimately)',
        'unresolved': 'well-formed ORCID iD with no record in the registry',
        'invalid_checksum': 'ORCID iD fails its ISO 7064 check digit — it cannot be a real iD',
    }[overall]
    return paper


def verify_authors(papers, progress_cb=None, max_lookups=None):
    """
    Resolves every distinct ORCID across `papers` (bounded), annotates
    each paper, and returns a summary for the run record.
    """
    papers = list(papers or [])
    if not papers:
        return {'orcids_checked': 0}

    max_lookups = ORCID_MAX_LOOKUPS if max_lookups is None else max_lookups
    wanted = []
    for paper in papers:
        for orcid in _paper_orcids(paper):
            if orcid not in wanted:
                wanted.append(orcid)

    # Checksum-fail iDs never cost a request.
    malformed = {o: {'found': False, 'orcid': o, 'checksum_valid': False,
                     'reason': 'ORCID check digit (ISO 7064 MOD 11-2) does not validate'}
                 for o in wanted if not checksum_valid(o)}
    resolvable = [o for o in wanted if o not in malformed][:max_lookups]

    results = dict(malformed)
    if resolvable:
        if progress_cb:
            progress_cb(f'Verifying {len(resolvable)} author ORCID iD(s) against the ORCID registry...')
        with ThreadPoolExecutor(max_workers=max(1, ORCID_MAX_WORKERS)) as pool:
            for orcid, result in zip(resolvable, pool.map(orcid_lookup, resolvable)):
                results[orcid] = result

    for paper in papers:
        annotate_paper(paper, results)

    status_counts = {}
    for paper in papers:
        status = paper.get('author_orcid_status') or 'not_checked'
        status_counts[status] = status_counts.get(status, 0) + 1

    return {
        'papers_with_orcid': sum(1 for p in papers if p.get('author_orcid_checks')),
        'papers_without_orcid': sum(1 for p in papers if not p.get('author_orcid_checks')),
        'orcids_seen': len(wanted),
        'orcids_checked': len(resolvable),
        'orcids_resolved': sum(1 for r in results.values() if r.get('found')),
        'orcids_invalid_checksum': len(malformed),
        'orcids_unresolved': sum(1 for r in results.values()
                                 if not r.get('found') and r.get('checksum_valid') and not r.get('error')),
        'orcids_over_budget': max(0, len(wanted) - len(malformed) - len(resolvable)),
        'author_status_counts': status_counts,
    }
