"""
Additional real academic-source integrations, beyond the core three
(ArXiv / Semantic Scholar / OpenAlex already in app.py).

Every function here either returns real papers from the source's actual
API, or an empty list — never synthetic/placeholder data. Two tiers:

FREE, NO KEY REQUIRED (always available):
    Crossref, PubMed/PMC (NCBI E-utilities), Europe PMC, DOAJ, ERIC,
    Internet Archive, INSPIRE-HEP

FREE BUT REQUIRE THE USER'S OWN REGISTERED API KEY (read from env vars —
same pattern as GROQ_API_KEY; unset means the source is skipped, not
faked):
    IEEE Xplore   -> IEEE_API_KEY      (register: developer.ieee.org)
    Springer Nature -> SPRINGER_API_KEY (register: dev.springernature.com)
    NASA ADS      -> ADS_API_KEY       (register: ui.adsabs.harvard.edu)
    CORE          -> CORE_API_KEY      (register: core.ac.uk/services/api)

Endpoints were verified against each provider's current public docs
before being wired in here (not guessed) — see the comment above each
function for the source checked.

NTRS (NASA Technical Reports Server), zbMATH Open, and AGRIS are
intentionally NOT included yet: their exact request schemas couldn't be
confirmed with enough confidence in this pass to ship a reliable
integration, so they're left out rather than shipped half-working.
"""
import os
import re
import time
import requests
import xml.etree.ElementTree as ET

TIMEOUT = 15
UA = {'User-Agent': 'ResearchLensAI/1.0 (mailto:megham112004@gmail.com)'}


def _clip(text, n=500):
    text = (text or '').strip()
    return text[:n] if text else 'No abstract available.'


def _strip_tags(text):
    return re.sub(r'<[^>]+>', '', text or '')


# ─── Free, no API key ───────────────────────────────────────────────

def fetch_crossref(topic, max_results=30, offset=0):
    """Crossref works search — the DOI registration agency most publishers
    (including IEEE, ACM, Elsevier, Springer, Wiley, ACS) submit metadata
    to. No key needed. Docs: https://api.crossref.org/swagger-ui/index.html

    `offset`: Crossref's works endpoint documents a real `offset` param for
    paging past the first `rows` results — used by deep_collection.py to
    page through a source instead of only ever seeing page one.

    Crossref also returns `is-referenced-by-count` on every work — a real,
    Crossref-maintained citation count (not derived/estimated here), so we
    surface it as `citation_count` for impact scoring instead of leaving
    citation data blank for every Crossref-sourced paper.
    """
    try:
        url = "https://api.crossref.org/works"
        params = {'query': topic, 'rows': min(max_results, 100), 'offset': max(0, offset), 'mailto': 'megham112004@gmail.com'}
        resp = requests.get(url, params=params, timeout=TIMEOUT, headers=UA)
        resp.raise_for_status()
        items = resp.json().get('message', {}).get('items', [])
        papers = []
        for it in items:
            title_list = it.get('title') or []
            title = (title_list[0] if title_list else '').strip()
            if not title:
                continue
            year = 'N/A'
            date_parts = (it.get('published') or it.get('published-print') or it.get('published-online') or {}).get('date-parts')
            if date_parts and date_parts[0]:
                year = str(date_parts[0][0])
            authors = []
            for a in (it.get('author') or [])[:3]:
                name = ' '.join(x for x in [a.get('given'), a.get('family')] if x)
                if name:
                    authors.append(name)
            papers.append({
                'title': title,
                'abstract': _clip(_strip_tags(it.get('abstract', ''))),
                'year': year,
                'authors': authors,
                'categories': ['Academic Paper'],
                'source': 'Crossref',
                'doi': it.get('DOI', ''),
                'venue': (it.get('container-title') or [''])[0],
                'citation_count': it.get('is-referenced-by-count'),
                'keywords_native': it.get('subject') or [],
                # Real Crossref ISBN, when this record is a book/chapter —
                # a genuine persistent identifier, never fabricated for a
                # record Crossref didn't provide one for.
                'isbn': (it.get('ISBN') or [''])[0],
            })
        try:
            from paper_quality import filter_credible
            papers = filter_credible(papers)   # drop junk/test deposits
        except ImportError:
            pass
        return papers
    except Exception as e:
        print(f"Crossref fetch failed: {e}")
        return []


def fetch_pubmed(topic, max_results=30, offset=0):
    """NCBI E-utilities (PubMed) — esearch for IDs, efetch for full XML
    records (title/abstract/authors/year in one pass). No key required,
    but NCBI rate-limits unkeyed traffic to ~3 req/sec.
    Docs: https://www.ncbi.nlm.nih.gov/books/NBK25501/

    `offset`: passed straight through to esearch's documented `retstart`
    param, so repeated calls with increasing offsets walk through the
    full PubMed result set instead of only ever returning the first page.
    PubMed has no citation-count field in this call (would need a
    separate NIH iCite lookup per paper), so citation_count is left unset
    here rather than guessed.
    """
    try:
        base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
        search_resp = requests.get(f"{base}/esearch.fcgi", params={
            'db': 'pubmed', 'term': topic, 'retmode': 'json',
            'retmax': min(max_results, 50), 'retstart': max(0, offset),
        }, timeout=TIMEOUT, headers=UA)
        search_resp.raise_for_status()
        ids = search_resp.json().get('esearchresult', {}).get('idlist', [])
        if not ids:
            return []
        fetch_resp = requests.get(f"{base}/efetch.fcgi", params={
            'db': 'pubmed', 'id': ','.join(ids), 'rettype': 'abstract', 'retmode': 'xml'
        }, timeout=TIMEOUT, headers=UA)
        fetch_resp.raise_for_status()
        root = ET.fromstring(fetch_resp.text)
        papers = []
        for article in root.findall('.//PubmedArticle'):
            title_el = article.find('.//ArticleTitle')
            title = ''.join(title_el.itertext()).strip() if title_el is not None else ''
            if not title:
                continue
            abstract_parts = [''.join(t.itertext()) for t in article.findall('.//AbstractText')]
            year_el = article.find('.//PubDate/Year')
            if year_el is None:
                year_el = article.find('.//PubDate/MedlineDate')
            year = year_el.text[:4] if year_el is not None and year_el.text else 'N/A'
            authors = []
            for a in article.findall('.//Author')[:3]:
                last = a.find('LastName')
                fore = a.find('ForeName')
                if last is not None:
                    name = f"{fore.text} {last.text}" if fore is not None else last.text
                    authors.append(name)
            doi_el = article.find(".//ArticleId[@IdType='doi']")
            pmid_el = article.find(".//ArticleId[@IdType='pubmed']")
            journal_el = article.find('.//Journal/Title')
            # PubMed's own <PublicationType> list — the authoritative
            # per-record type vocabulary ("Journal Article", "Review",
            # "Editorial", "Retracted Publication", "Comment", ...).
            # paper_verification uses this for both publication-type
            # classification and the retraction check, so it has to be
            # read out of the record rather than inferred from the venue.
            pub_types = [''.join(t.itertext()).strip()
                         for t in article.findall('.//PublicationType')]
            papers.append({
                'title': title,
                'abstract': _clip(' '.join(abstract_parts)),
                'year': year,
                'authors': authors,
                'categories': ['Medicine/Life Sciences'],
                'source': 'PubMed',
                'doi': doi_el.text if doi_el is not None else '',
                # Real field from the same XML record — PubMed's
                # <Journal><Title> element. Previously not captured, which
                # meant a PubMed-only paper could never get the real
                # OpenAlex venue-influence bonus even for a well-known
                # journal, simply because the journal name was never read
                # out of PubMed's own response.
                'venue': journal_el.text if journal_el is not None and journal_el.text else '',
                'pmid': pmid_el.text if pmid_el is not None else '',
                'publication_types_native': [t for t in pub_types if t],
            })
        return papers
    except Exception as e:
        print(f"PubMed fetch failed: {e}")
        return []


def fetch_europe_pmc(topic, max_results=30, cursor_mark='*'):
    """Europe PMC REST API — biomedical + life sciences, includes PMC full-
    text-linked records. No key required.
    Docs: https://europepmc.org/RestfulWebService

    Returns just the papers list (unchanged signature for every existing
    caller that treats every source function alike). Europe PMC's
    documented way to page past the first `pageSize` results is
    `cursorMark` (start with '*', then reuse the `nextCursorMark` the API
    hands back) rather than a numeric offset — deep_collection.py that
    needs the next-page cursor calls `fetch_europe_pmc_page` below
    instead of this one. Also requests `citedByCount` — a real,
    Europe-PMC-maintained citation count — via resultType=core.
    """
    papers, _ = fetch_europe_pmc_page(topic, max_results=max_results, cursor_mark=cursor_mark)
    return papers


def fetch_europe_pmc_page(topic, max_results=30, cursor_mark='*'):
    """Same call as fetch_europe_pmc, but also returns the `nextCursorMark`
    Europe PMC hands back, so a caller doing real pagination (deep_collection.py)
    knows what cursor to send for the next page. Returns (papers, next_cursor);
    next_cursor is None on failure or when there's nothing further to page."""
    try:
        url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
        params = {
            'query': topic, 'format': 'json', 'pageSize': min(max_results, 100),
            'cursorMark': cursor_mark or '*', 'resultType': 'core',
        }
        resp = requests.get(url, params=params, timeout=TIMEOUT, headers=UA)
        resp.raise_for_status()
        data = resp.json()
        results = data.get('resultList', {}).get('result', [])
        next_cursor = data.get('nextCursorMark')
        papers = []
        for r in results:
            title = (r.get('title') or '').strip()
            if not title:
                continue
            authors = [a.strip() for a in (r.get('authorString') or '').split(',')[:3] if a.strip()]
            papers.append({
                'title': title,
                'abstract': _clip(r.get('abstractText', '')),
                'year': str(r.get('pubYear') or 'N/A'),
                'authors': authors,
                'categories': ['Medicine/Life Sciences'],
                'source': 'Europe PMC',
                'doi': r.get('doi', ''),
                'citation_count': r.get('citedByCount'),
                # Real fields Europe PMC returns for every result — used
                # by full_text_analysis.py to fetch a paper's REAL full
                # text (not the abstract) when it's genuinely open access,
                # rather than guessing at content from the abstract alone.
                'pmcid': r.get('pmcid', ''),
                'is_open_access': (r.get('isOpenAccess') == 'Y'),
                # Real field — Europe PMC's own 'journalTitle' (present on
                # `resultType=core` responses). Previously not captured.
                'venue': r.get('journalTitle', ''),
            })
        return papers, next_cursor
    except Exception as e:
        print(f"Europe PMC fetch failed: {e}")
        return [], None


def fetch_doaj(topic, max_results=30, page=1):
    """Directory of Open Access Journals — open-access journal articles
    across all fields. No key required.
    Docs: https://doaj.org/api/docs

    `page`: DOAJ's search endpoint documents a 1-based `page` query param
    alongside `pageSize` for walking through result pages beyond the first.
    DOAJ doesn't expose a citation count, so citation_count stays unset.
    """
    try:
        url = f"https://doaj.org/api/search/articles/{requests.utils.quote(topic)}"
        resp = requests.get(url, params={'pageSize': min(max_results, 100), 'page': max(1, page)}, timeout=TIMEOUT, headers=UA)
        resp.raise_for_status()
        results = resp.json().get('results', [])
        papers = []
        for r in results:
            bib = r.get('bibjson', {})
            title = (bib.get('title') or '').strip()
            if not title:
                continue
            authors = [a.get('name', '') for a in (bib.get('author') or [])[:3] if a.get('name')]
            doi = next((i.get('id', '') for i in (bib.get('identifier') or []) if i.get('type') == 'doi'), '')
            papers.append({
                'title': title,
                'abstract': _clip(bib.get('abstract', '')),
                'year': str(bib.get('year') or 'N/A'),
                'authors': authors,
                'categories': ['Open Access'],
                'source': 'DOAJ',
                'doi': doi,
                'keywords_native': bib.get('keywords') or [],
                # Real field — DOAJ's bibjson.journal.title.
                'venue': (bib.get('journal') or {}).get('title', ''),
            })
        return papers
    except Exception as e:
        print(f"DOAJ fetch failed: {e}")
        return []


def fetch_eric(topic, max_results=30, start=0):
    """ERIC (Education Resources Information Center), U.S. Dept. of
    Education / IES — education research literature. No key required.
    Docs: https://eric.ed.gov/?api

    `start`: ERIC's API is Solr-backed and documents a `start` param
    (paired with `rows`) for offset pagination. No citation-count field
    is exposed, so citation_count stays unset rather than guessed.
    """
    try:
        url = "https://api.ies.ed.gov/eric/"
        resp = requests.get(url, params={'search': topic, 'format': 'json',
                                          'rows': min(max_results, 100), 'start': max(0, start)},
                             timeout=TIMEOUT, headers=UA)
        resp.raise_for_status()
        docs = resp.json().get('response', {}).get('docs', [])
        papers = []
        for d in docs:
            title = (d.get('title') or '').strip()
            if not title:
                continue
            authors = (d.get('author') or [])[:3]
            papers.append({
                'title': title,
                'abstract': _clip(d.get('description', '')),
                'year': str(d.get('publicationdateyear') or 'N/A'),
                'authors': authors,
                'categories': ['Education'],
                'source': 'ERIC',
                'doi': '',  # ERIC's schema genuinely has no DOI field — its records use ED/EJ accession numbers instead, never guessed at
                # Real field — ERIC's own 'source' field (the journal or
                # publication name for EJ-type records; may be blank for
                # ED-type grey-literature records, which genuinely have no
                # publication venue to report).
                'venue': d.get('source', '') or '',
            })
        return papers
    except Exception as e:
        print(f"ERIC fetch failed: {e}")
        return []


def fetch_internet_archive(topic, max_results=30, page=1):
    """Internet Archive advanced search — texts collection, useful for
    humanities/history sources and grey literature. No key required.
    Docs: https://archive.org/advancedsearch.php

    `page`: advancedsearch.php documents `rows` + `page` (1-based) for
    stepping through results beyond the first page. No citation-count
    concept applies to this collection, so it's left unset.
    """
    try:
        url = "https://archive.org/advancedsearch.php"
        params = {
            'q': f'{topic} AND mediatype:texts',
            'fl[]': ['identifier', 'title', 'description', 'year', 'creator'],
            'rows': min(max_results, 50), 'page': max(1, page), 'output': 'json',
        }
        resp = requests.get(url, params=params, timeout=TIMEOUT, headers=UA)
        resp.raise_for_status()
        docs = resp.json().get('response', {}).get('docs', [])
        papers = []
        for d in docs:
            title = (d.get('title') or '').strip()
            if not title:
                continue
            creator = d.get('creator')
            authors = creator if isinstance(creator, list) else ([creator] if creator else [])
            papers.append({
                'title': title,
                'abstract': _clip(d.get('description', '') if isinstance(d.get('description'), str) else ''),
                'year': str(d.get('year') or 'N/A'),
                'authors': authors[:3],
                'categories': ['Archive/Humanities'],
                'source': 'Internet Archive',
                'doi': '',
            })
        return papers
    except Exception as e:
        print(f"Internet Archive fetch failed: {e}")
        return []


def fetch_inspire_hep(topic, max_results=30, page=1):
    """INSPIRE-HEP — high-energy/particle physics literature database.
    No key required. Docs: https://inspirehep.net/info/hep/api

    `page`: INSPIRE's literature endpoint documents `size` + `page`
    (1-based) for pagination. It also tracks a real `citation_count` per
    record (INSPIRE's own citation graph for HEP papers), requested here
    via the `fields` param and surfaced as `citation_count` for scoring.
    """
    try:
        url = "https://inspirehep.net/api/literature"
        params = {'q': topic, 'size': min(max_results, 50), 'page': max(1, page),
                  'fields': 'titles,abstracts,publication_info,authors,citation_count,keywords,dois'}
        resp = requests.get(url, params=params, timeout=TIMEOUT, headers=UA)
        resp.raise_for_status()
        hits = resp.json().get('hits', {}).get('hits', [])
        papers = []
        for h in hits:
            meta = h.get('metadata', {})
            # INSPIRE's own top-level literature-record ID (distinct from
            # the metadata block) — a real persistent identifier for HEP
            # records that may have no DOI at all (proceedings, reports).
            inspire_id = str(h.get('id', '')) if h.get('id') is not None else ''
            titles = meta.get('titles') or []
            title = (titles[0].get('title', '') if titles else '').strip()
            if not title:
                continue
            abstracts = meta.get('abstracts') or []
            abstract = abstracts[0].get('value', '') if abstracts else ''
            pub_info = meta.get('publication_info') or []
            year = str(pub_info[0].get('year', 'N/A')) if pub_info else 'N/A'
            authors = [a.get('full_name', '') for a in (meta.get('authors') or [])[:3]]
            keywords = [k.get('value', '') for k in (meta.get('keywords') or []) if k.get('value')]
            # Real fields — INSPIRE's own 'dois' array (many, not all, HEP
            # records have one) and publication_info[0].journal_title.
            dois = meta.get('dois') or []
            doi = dois[0].get('value', '') if dois else ''
            journal_title = pub_info[0].get('journal_title', '') if pub_info else ''
            papers.append({
                'title': title,
                'abstract': _clip(abstract),
                'year': year,
                'authors': authors,
                'categories': ['Physics'],
                'source': 'INSPIRE-HEP',
                'doi': doi,
                'venue': journal_title,
                'citation_count': meta.get('citation_count'),
                'keywords_native': keywords,
                'inspire_id': inspire_id,
            })
        return papers
    except Exception as e:
        print(f"INSPIRE-HEP fetch failed: {e}")
        return []


# ─── Require the user's own free API key ───────────────────────────

def fetch_ieee_xplore(topic, max_results=30, start_record=1):
    """IEEE Xplore Metadata Search API. Needs a free key from
    developer.ieee.org, set as IEEE_API_KEY. Returns [] if unset —
    never fabricates IEEE results without one.
    Docs: https://developer.ieee.org/

    `start_record`: IEEE's Metadata Search API documents a 1-based
    `start_record` param for paging past the first `max_records`. It also
    returns `citing_paper_count` per article — IEEE's own citation count
    — surfaced here as `citation_count`.
    """
    api_key = os.environ.get('IEEE_API_KEY')
    if not api_key:
        return []
    try:
        url = "https://ieeexploreapi.ieee.org/api/v1/search/articles"
        params = {'apikey': api_key, 'querytext': topic, 'max_records': min(max_results, 50),
                   'start_record': max(1, start_record)}
        resp = requests.get(url, params=params, timeout=TIMEOUT, headers=UA)
        resp.raise_for_status()
        articles = resp.json().get('articles', [])
        papers = []
        for a in articles:
            title = (a.get('title') or '').strip()
            if not title:
                continue
            authors = [au.get('full_name', '') for au in (a.get('authors', {}).get('authors') or [])[:3]]
            papers.append({
                'title': title,
                'abstract': _clip(a.get('abstract', '')),
                'year': str(a.get('publication_year') or 'N/A'),
                'authors': authors,
                'categories': ['Engineering/CS'],
                'source': 'IEEE Xplore',
                'doi': a.get('doi', ''),
                'venue': a.get('publication_title', ''),
                'citation_count': a.get('citing_paper_count'),
            })
        return papers
    except Exception as e:
        print(f"IEEE Xplore fetch failed: {e}")
        return []


def fetch_springer(topic, max_results=30, start=1):
    """Springer Nature Meta API v2. Needs a free key from
    dev.springernature.com, set as SPRINGER_API_KEY. Returns [] if unset.
    Docs: https://dev.springernature.com/docs/api-endpoints/meta-api/

    `start`: the Meta API documents a 1-based `s` (start index) param
    alongside `p` (page size) for pagination. Springer's Meta API doesn't
    expose a citation count, so citation_count stays unset here.
    """
    api_key = os.environ.get('SPRINGER_API_KEY')
    if not api_key:
        return []
    try:
        url = "https://api.springernature.com/meta/v2/json"
        params = {'q': topic, 'api_key': api_key, 'p': min(max_results, 50), 's': max(1, start)}
        resp = requests.get(url, params=params, timeout=TIMEOUT, headers=UA)
        resp.raise_for_status()
        records = resp.json().get('records', [])
        papers = []
        for r in records:
            title = (r.get('title') or '').strip()
            if not title:
                continue
            authors = [c.get('creator', '') for c in (r.get('creators') or [])[:3]]
            year = (r.get('publicationDate') or 'N/A')[:4] if r.get('publicationDate') else 'N/A'
            papers.append({
                'title': title,
                'abstract': _clip(r.get('abstract', '')),
                'year': year,
                'authors': authors,
                'categories': ['Academic Paper'],
                'source': 'Springer Nature',
                'doi': r.get('doi', ''),
                'venue': r.get('publicationName', ''),
            })
        return papers
    except Exception as e:
        print(f"Springer Nature fetch failed: {e}")
        return []


def fetch_nasa_ads(topic, max_results=30, offset=0):
    """NASA ADS search API. Needs a free token from ui.adsabs.harvard.edu
    (Account settings -> API Token), set as ADS_API_KEY. Returns [] if
    unset. Docs: https://ui.adsabs.harvard.edu/help/api/

    `offset`: ADS's Solr-backed search documents `start` + `rows` for
    pagination. Also requests `citation_count`, a real field ADS tracks
    per record, surfaced here for impact scoring.
    """
    api_key = os.environ.get('ADS_API_KEY')
    if not api_key:
        return []
    try:
        url = "https://api.adsabs.harvard.edu/v1/search/query"
        params = {'q': topic, 'rows': min(max_results, 50), 'start': max(0, offset),
                   'fl': 'title,abstract,year,author,citation_count,doi,pub'}
        resp = requests.get(url, params=params, timeout=TIMEOUT,
                             headers={**UA, 'Authorization': f'Bearer {api_key}'})
        resp.raise_for_status()
        docs = resp.json().get('response', {}).get('docs', [])
        papers = []
        for d in docs:
            titles = d.get('title') or []
            title = (titles[0] if titles else '').strip()
            if not title:
                continue
            # Real fields — ADS's own 'doi' (a list; many but not all
            # astro papers have one) and 'pub' (the publication/venue
            # name). Previously requested neither.
            dois = d.get('doi') or []
            papers.append({
                'title': title,
                'abstract': _clip(d.get('abstract', '')),
                'year': str(d.get('year') or 'N/A'),
                'authors': (d.get('author') or [])[:3],
                'categories': ['Physics/Astronomy'],
                'source': 'NASA ADS',
                'doi': dois[0] if dois else '',
                'venue': d.get('pub', ''),
                'citation_count': d.get('citation_count'),
            })
        return papers
    except Exception as e:
        print(f"NASA ADS fetch failed: {e}")
        return []


def fetch_core(topic, max_results=30, offset=0):
    """CORE API v3 — world's largest open-access aggregator. Needs a free
    key from core.ac.uk/services/api, set as CORE_API_KEY. Returns [] if
    unset (CORE's v3 API 429s all unauthenticated requests, so there's no
    degraded "keyless" mode to fall back to).
    Docs: https://api.core.ac.uk/docs/v3

    `offset`: CORE v3's search endpoint documents `offset` + `limit` for
    pagination. CORE doesn't expose a citation count in this response,
    so citation_count stays unset.
    """
    api_key = os.environ.get('CORE_API_KEY')
    if not api_key:
        return []
    try:
        url = "https://api.core.ac.uk/v3/search/works"
        resp = requests.get(url, params={'q': topic, 'limit': min(max_results, 50), 'offset': max(0, offset)}, timeout=TIMEOUT,
                             headers={**UA, 'Authorization': f'Bearer {api_key}'})
        resp.raise_for_status()
        results = resp.json().get('results', [])
        papers = []
        for r in results:
            title = (r.get('title') or '').strip()
            if not title:
                continue
            authors = [a.get('name', '') for a in (r.get('authors') or [])[:3]]
            year = str(r.get('yearPublished') or (r.get('publishedDate') or 'N/A')[:4])
            papers.append({
                'title': title,
                'abstract': _clip(r.get('abstract', '')),
                'year': year,
                'authors': authors,
                'categories': ['Open Access'],
                'source': 'CORE',
                'doi': r.get('doi', ''),
                # Real field — CORE v3's own 'publisher' string on the work
                # object. Used as CORE's venue-equivalent since a distinct
                # journal-title field isn't consistently present across its
                # aggregated (often repository-hosted) records.
                'venue': r.get('publisher', '') or '',
                # CORE's own work ID — full_text_analysis.py uses this to
                # check /works/{id} for a genuine `fullText` field CORE has
                # actually ingested, instead of guessing paper content from
                # the abstract.
                'core_id': str(r.get('id', '')) if r.get('id') is not None else '',
            })
        return papers
    except Exception as e:
        print(f"CORE fetch failed: {e}")
        return []


def fetch_sciencedirect(topic, max_results=25, offset=0):
    """
    Elsevier ScienceDirect Search API v2. Needs a free key from
    dev.elsevier.com (Elsevier Developer Portal — free tier, personal
    use), set as SCIENCEDIRECT_API_KEY. Returns [] if unset — same
    no-fake-data rule as every other keyed source here.
    Docs: https://dev.elsevier.com/sd_search_tips.html

    `offset`: the Search API documents `offset` alongside `count` (page
    size, capped at 100) for pagination.

    IMPORTANT, stated plainly rather than glossed over: ScienceDirect's
    Search API does NOT return abstracts in search results (Elsevier's
    licensing restricts full abstract text to entitled subscribers, even
    via API) — so `abstract` is deliberately left as an empty string
    here rather than something invented to fill it. Title, authors, DOI,
    date, and venue ARE returned and are used. This has not been tested
    against a live key (no test key was available while building this) —
    if Elsevier's exact field names differ from what's coded here, the
    request will fail safely and this function returns [] via the
    except block below, the same as any other source having a bad day,
    rather than crash the rest of the pipeline.
    """
    api_key = os.environ.get('SCIENCEDIRECT_API_KEY')
    if not api_key:
        return []
    try:
        url = "https://api.elsevier.com/content/search/sciencedirect"
        resp = requests.get(
            url,
            params={'query': topic, 'count': min(max_results, 100), 'offset': max(0, offset)},
            timeout=TIMEOUT,
            headers={**UA, 'X-ELS-APIKey': api_key, 'Accept': 'application/json'},
        )
        resp.raise_for_status()
        entries = resp.json().get('search-results', {}).get('entry', [])
        papers = []
        for e in entries:
            title = (e.get('dc:title') or '').strip()
            if not title:
                continue
            creator = e.get('dc:creator', '')
            authors = [creator] if isinstance(creator, str) and creator else []
            date = e.get('prism:coverDate') or e.get('load-date') or ''
            papers.append({
                'title': title,
                'abstract': '',  # not returned by this API — see docstring; never fabricated
                'year': (date[:4] if date else 'N/A'),
                'authors': authors,
                'categories': ['Academic Paper'],
                'source': 'ScienceDirect',
                'doi': e.get('prism:doi', ''),
                'venue': e.get('prism:publicationName', ''),
            })
        return papers
    except Exception as e:
        print(f"ScienceDirect fetch failed: {e}")
        return []


# ─── Per-venue, free, no API key ─────────────────────────────────────

# DBLP's own internal "stream" id for each venue we expose as a Specific
# Venue checkbox. These are DBLP's actual publication-series identifiers
# (dblp.org/db/conf/<id>/), not guesses — e.g. dblp.org/db/conf/aaai/.
DBLP_VENUE_STREAMS = {
    'AAAI': 'conf/aaai',
    'NeurIPS': 'conf/nips',   # DBLP kept the historical NIPS stream id
    'ICML': 'conf/icml',
    'ICLR': 'conf/iclr',
    'CVPR': 'conf/cvpr',
    'ICCV': 'conf/iccv',
    'ECCV': 'conf/eccv',
}


# ─── DBLP venue search ───────────────────────────────────────────────
# dblp.org's old search API now sits behind a bot-challenge (Anubis), so
# this queries DBLP's official public SPARQL service instead, with the
# old search API kept only as a last-resort fallback.

DBLP_SPARQL_URL = "https://sparql.dblp.org/sparql"
DBLP_LEGACY_SEARCH_URL = "https://dblp.org/search/publ/api"

_DBLP_STOPWORDS = {
    'a', 'an', 'the', 'of', 'for', 'and', 'or', 'in', 'on', 'to', 'with',
    'using', 'based', 'via', 'by', 'from', 'at', 'is', 'are',
}


class DblpBlockedError(RuntimeError):
    """DBLP answered with a bot-challenge page instead of data."""


def _dblp_request_json(method, url, *, params=None, data=None,
                       accept='application/json', max_attempts=3, timeout=30):
    """
    One DBLP HTTP call that returns parsed JSON, or raises with a message
    that says what actually came back (never the bare JSON error).
      * 429 / 5xx -> wait (honouring Retry-After) and retry
      * bot-challenge HTML page -> DblpBlockedError immediately
      * any other non-JSON body -> RuntimeError with status/type/snippet
    """
    headers = dict(UA)
    headers['Accept'] = accept
    host = url.split('/')[2]
    last = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.request(method, url, params=params, data=data,
                                    headers=headers, timeout=timeout)
        except requests.exceptions.RequestException as e:
            last = RuntimeError(f"{host}: network error ({type(e).__name__}: {e})")
            if attempt < max_attempts:
                time.sleep(1.5 * (2 ** attempt))
            continue

        if resp.status_code == 429 or resp.status_code >= 500:
            last = RuntimeError(f"{host}: HTTP {resp.status_code}")
            if attempt < max_attempts:
                wait_s = float(resp.headers.get('Retry-After', 0) or 0) or (1.5 * (2 ** attempt))
                time.sleep(min(wait_s, 20))
            continue

        try:
            return resp.json()
        except ValueError:
            body = (resp.text or '').strip()
            if 'not a bot' in body.lower() or 'anubis' in body.lower():
                raise DblpBlockedError(
                    f"{host} is serving a bot-challenge page (Anubis) instead of data")
            ctype = resp.headers.get('Content-Type', '?')
            snippet = body.replace('\n', ' ')[:120] or '<empty body>'
            raise RuntimeError(
                f"{host}: HTTP {resp.status_code} but not JSON (Content-Type={ctype}; body starts: {snippet!r})")
    raise last or RuntimeError(f"{host}: request failed")


def _dblp_topic_terms(topic):
    words = re.findall(r"[a-z0-9]+", (topic or '').lower())
    terms = [w for w in words if len(w) >= 2 and w not in _DBLP_STOPWORDS]
    return (terms or words)[:6]


def _dblp_sparql_query(stream, terms, limit, offset):
    """
    Papers in one DBLP stream whose title contains ALL topic terms
    (same AND behaviour as DBLP's own search). Terms are only [a-z0-9],
    so they are safe to embed. Terms of <=3 letters ("ai") must match a
    whole word; longer terms match as a word prefix.
    """
    bs = "\\\\"  # two backslashes inside the SPARQL string = one regex backslash
    filters = []
    for t in terms:
        pat = f"{bs}b{t}{bs}b" if len(t) <= 3 else f"{bs}b{t}"
        filters.append(f'  FILTER(REGEX(STR(?title), "{pat}", "i"))')
    return (
        "PREFIX dblp: <https://dblp.org/rdf/schema#>\n"
        "SELECT ?paper ?title ?year (SAMPLE(?doi_) AS ?doi)\n"
        '       (GROUP_CONCAT(DISTINCT ?name; separator="|") AS ?authors) WHERE {\n'
        f"  ?paper dblp:publishedInStream <https://dblp.org/streams/{stream}> .\n"
        "  ?paper dblp:title ?title .\n"
        "  ?paper dblp:yearOfPublication ?year .\n"
        + "\n".join(filters) + "\n"
        "  OPTIONAL { ?paper dblp:doi ?doi_ }\n"
        "  OPTIONAL { ?paper dblp:authoredBy ?a . ?a dblp:primaryCreatorName ?name }\n"
        "}\n"
        "GROUP BY ?paper ?title ?year\n"
        f"ORDER BY DESC(?year) ?title LIMIT {int(limit)} OFFSET {int(offset)}"
    )


def _dblp_paper(venue_name, title, year, authors, doi, key, venue=None):
    return {
        'title': title,
        'abstract': 'No abstract available (DBLP does not provide abstracts).',
        'year': str(year or 'N/A'),
        'authors': authors[:3],
        'categories': ['Academic Paper'],
        'source': f'{venue_name} (via DBLP)',
        'doi': doi or '',
        'venue': venue or venue_name,
        'dblp_key': key or '',
    }


def _dblp_run_sparql(venue_name, stream, terms, limit, offset):
    """One SPARQL query for one set of title terms (ALL must match)."""
    q = _dblp_sparql_query(stream, terms, limit, offset)
    data = _dblp_request_json('POST', DBLP_SPARQL_URL, data={'query': q},
                              accept='application/sparql-results+json')
    papers = []
    for row in (data.get('results') or {}).get('bindings') or []:
        val = lambda k: (row.get(k) or {}).get('value', '')
        title = val('title').rstrip('.').strip()
        if not title:
            continue
        doi = val('doi')
        if doi.startswith('https://doi.org/'):
            doi = doi[len('https://doi.org/'):]
        authors = [a for a in val('authors').split('|') if a]
        key = val('paper').replace('https://dblp.org/rec/', '')
        papers.append(_dblp_paper(venue_name, title, val('year'), authors, doi, key))
    return papers


def _dblp_via_sparql(topic, venue_name, stream, max_results, first):
    """
    DBLP matching is literal: every topic term must appear in the title.
    A natural-language topic ("Artificial Intelligence", or a topic plus an
    idea description) can therefore legitimately match ZERO papers in a
    venue even though DBLP is healthy. When the all-terms query is empty
    (first page only), fall back to the longest terms one at a time and
    merge the results. Every paper returned is still a real DBLP record
    from that venue's stream; relevance scoring downstream decides what is
    actually on-topic.
    """
    terms = _dblp_topic_terms(topic)
    limit = min(max_results, 100)
    papers = _dblp_run_sparql(venue_name, stream, terms, limit, max(0, first))
    if papers or first > 0 or len(set(terms)) < 2:
        return papers

    merged, seen = [], set()
    for t in sorted(set(terms), key=len, reverse=True)[:3]:
        time.sleep(1.0)   # be polite to the public endpoint
        try:
            got = _dblp_run_sparql(venue_name, stream, [t], limit, 0)
        except Exception:
            if merged:
                break      # keep what we already have
            raise
        for p in got:
            k = p.get('dblp_key') or p['title'].lower()
            if k not in seen:
                seen.add(k)
                merged.append(p)
        if len(merged) >= limit:
            break
    if merged:
        print(f"{venue_name} (via DBLP): no title contained all of {terms}; "
              f"used single-term fallback, {len(merged)} papers")
    return merged[:limit]


def _dblp_via_legacy_search(topic, venue_name, stream, max_results, first):
    """Old search API. Only works if DBLP's bot challenge is off."""
    params = {'q': f"{topic} streamid:{stream}:", 'format': 'json',
              'h': min(max_results, 100), 'f': max(0, first)}
    data = _dblp_request_json('GET', DBLP_LEGACY_SEARCH_URL, params=params,
                              max_attempts=1, timeout=TIMEOUT)
    hits = ((data.get('result') or {}).get('hits') or {}).get('hit') or []
    papers = []
    for h in hits:
        info = h.get('info', {}) or {}
        title = (info.get('title') or '').rstrip('.').strip()
        if not title:
            continue
        author_field = (info.get('authors') or {}).get('author', [])
        if isinstance(author_field, dict):
            author_field = [author_field]
        authors = [(a.get('text') if isinstance(a, dict) else a) for a in author_field]
        papers.append(_dblp_paper(venue_name, title, info.get('year'), authors,
                                  info.get('doi', ''), info.get('key', ''),
                                  info.get('venue', venue_name)))
    return papers


def fetch_dblp_venue(topic, venue_name, max_results=30, first=0):
    """
    Per-venue publication search via DBLP (no key). Tries DBLP's public
    SPARQL service first; falls back to the legacy search API. If both
    fail, raises RuntimeError that says why (e.g. bot-challenge page).
    DBLP has no abstracts, so `abstract` is a placeholder, never faked.
    """
    stream = DBLP_VENUE_STREAMS.get(venue_name)
    if not stream:
        return []
    errors = []
    for label, fn in (('SPARQL', _dblp_via_sparql), ('search API', _dblp_via_legacy_search)):
        try:
            return fn(topic, venue_name, stream, max_results, first)
        except Exception as e:
            errors.append(f"{label}: {e}")
    raise RuntimeError("DBLP unavailable — " + " | ".join(errors))


# ─── Registry ───────────────────────────────────────────────────────

# Canonical source-name -> fetch function. Used by app.py's
# fetch_papers_multi_source to build its combined `all_sources` dict, and
# by the /api/paper-sources/status endpoint to report which optional-key
# sources are actually configured in this deployment.
FREE_SOURCES = {
    'Crossref': fetch_crossref,
    'PubMed': fetch_pubmed,
    'Europe PMC': fetch_europe_pmc,
    'DOAJ': fetch_doaj,
    'ERIC': fetch_eric,
    'Internet Archive': fetch_internet_archive,
    'INSPIRE-HEP': fetch_inspire_hep,
}

KEYED_SOURCES = {
    'IEEE Xplore': fetch_ieee_xplore,
    'Springer Nature': fetch_springer,
    'NASA ADS': fetch_nasa_ads,
    'CORE': fetch_core,
    'ScienceDirect': fetch_sciencedirect,
}

KEYED_SOURCE_ENV_VARS = {
    'IEEE Xplore': 'IEEE_API_KEY',
    'Springer Nature': 'SPRINGER_API_KEY',
    'NASA ADS': 'ADS_API_KEY',
    'CORE': 'CORE_API_KEY',
    'ScienceDirect': 'SCIENCEDIRECT_API_KEY',
}

# ─── Domain -> source relevance (flowchart stage 2) ──────────────────
#
# Most sources here are genuinely general-purpose (Semantic Scholar,
# OpenAlex, Crossref, arXiv, DOAJ, Internet Archive, IEEE Xplore,
# Springer Nature, ScienceDirect, CORE all index across many fields)
# and stay in the registry for every domain. A handful are real,
# well-known SUBJECT-SPECIFIC databases — querying them for an
# obviously unrelated domain wastes a full page-until-exhaustion cycle
# for near-zero real results. These keyword lists describe each
# specialized source's actual documented scope (PubMed/Europe PMC are
# biomedical literature databases; ERIC is the US education-research
# index; INSPIRE-HEP and NASA ADS are physics/astrophysics literature
# databases) — not an LLM guess about what's "relevant".
SPECIALIZED_SOURCE_DOMAINS = {
    'PubMed': ('medic', 'health', 'clinical', 'biomed', 'disease', 'patient', 'drug',
               'pharma', 'cancer', 'nursing', 'epidemiolog', 'therapy', 'diagnos',
               'surgical', 'vaccine', 'genomic', 'physiolog'),
    'Europe PMC': ('medic', 'health', 'clinical', 'biomed', 'disease', 'patient', 'drug',
                   'pharma', 'cancer', 'life science', 'biolog', 'genomic', 'molecular',
                   'genetic', 'protein', 'cell biology', 'microbiolog'),
    'ERIC': ('education', 'pedagog', 'curriculum', 'teacher', 'classroom', 'learning outcome',
             'student achievement', 'instructional', 'e-learning', 'literacy'),
    'INSPIRE-HEP': ('physics', 'particle physics', 'quantum field', 'cosmology',
                     'high energy', 'string theory', 'nuclear physics', 'gauge theory'),
    'NASA ADS': ('astro', 'astrophysics', 'planetary', 'space', 'cosmology', 'stellar',
                 'exoplanet', 'galax', 'orbital', 'telescope'),
}


def relevant_specialized_sources(text_blob):
    """
    Returns the subset of SPECIALIZED_SOURCE_DOMAINS whose keyword list
    matches somewhere in `text_blob` (already lowercased domain/
    sub-domain/topic/keywords/research-question text). Real substring
    matching against each source's documented actual scope — never an
    LLM guess about which database "should" apply. Callers decide what
    to do when the result is empty (ambiguous domain): the safe default
    used in app.py's _build_deep_source_registry is to include every
    specialized source rather than risk silently under-collecting for
    a domain this keyword list doesn't recognize.
    """
    text_blob = (text_blob or '').lower()
    return {source for source, terms in SPECIALIZED_SOURCE_DOMAINS.items()
            if any(term in text_blob for term in terms)}


def keyed_source_status():
    """{'IEEE Xplore': True/False, ...} — whether each key-requiring
    source has its env var set in this deployment right now."""
    return {name: bool(os.environ.get(var)) for name, var in KEYED_SOURCE_ENV_VARS.items()}