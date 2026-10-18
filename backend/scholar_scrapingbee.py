"""
scholar_scrapingbee.py -- Google Scholar through ScrapingBee.

Google Scholar has no official API. ScrapingBee fetches the Scholar page for
you (proxies / CAPTCHA handling) and returns the HTML, which this module
parses into normalized records. Request format follows ScrapingBee's own
published guide: GET https://app.scrapingbee.com/api/v1/ with
`url=<scholar url>` and `custom_google=true` (required by ScrapingBee for
Google domains).

Two uses in this app
--------------------
1. A paper SOURCE ("Google Scholar") selectable like arXiv/OpenAlex.
2. A GAP REALITY CHECK: for a detected gap, search Scholar for the gap's own
   wording and show the closest existing work, so a gap that is already
   addressed gets flagged. This is a *text-overlap signal for a human to
   review*, never proof that a gap is real or already solved.

Honest limits (also surfaced to the UI)
---------------------------------------
* Scholar shows a snippet, not an abstract. Records carry `abstract` =
  snippet and `abstract_is_snippet=True`.
* Scholar results usually have no DOI, so paper_verification cannot
  authenticate them by DOI; they will come back UNVERIFIED, not rejected.
* Every request costs ScrapingBee credits. Hard caps below protect them.
* Scraping Google is a grey area under Google's terms; use modestly.

Config (env / backend/.env)
---------------------------
SCRAPINGBEE_API_KEY            required to enable everything here
SCHOLAR_MAX_REQUESTS_PER_DAY   default 100  (hard cap, resets on UTC date change)
SCHOLAR_PAGE_DELAY_S           default 5    (pause between pages)
SCHOLAR_SOURCE_MAX_PAGES       default 2    (pages when used as a paper source)
SCHOLAR_GAPS_CHECKED_MAX       default 5    (gaps checked per click-all)
SCHOLAR_CACHE_TTL_S            default 86400
"""
import datetime
import os
import re
import threading
import time
from urllib.parse import quote_plus

import requests

try:
    from bs4 import BeautifulSoup
except ImportError:  # surfaced as a clear error at call time, not at import
    BeautifulSoup = None

SB_ENDPOINT = 'https://app.scrapingbee.com/api/v1/'
SCHOLAR_URL = 'https://scholar.google.com/scholar'

TIMEOUT_S = float(os.environ.get('SCHOLAR_REQUEST_TIMEOUT_S', '90'))
PAGE_DELAY_S = float(os.environ.get('SCHOLAR_PAGE_DELAY_S', '5'))
MAX_REQ_PER_DAY = int(os.environ.get('SCHOLAR_MAX_REQUESTS_PER_DAY', '100'))
SOURCE_MAX_PAGES = int(os.environ.get('SCHOLAR_SOURCE_MAX_PAGES', '2'))
GAPS_CHECKED_MAX = int(os.environ.get('SCHOLAR_GAPS_CHECKED_MAX', '5'))
CACHE_TTL_S = float(os.environ.get('SCHOLAR_CACHE_TTL_S', '86400'))


class ScholarError(Exception):
    """Any failure with a message safe to show the user."""


class ScholarNotConfigured(ScholarError):
    pass


class ScholarBlocked(ScholarError):
    """Google served a CAPTCHA / consent / unusual-traffic page."""


_lock = threading.Lock()
_usage = {'day': None, 'count': 0, 'total': 0, 'last_error': ''}
_cache = {}          # (query, start, ylo, yhi) -> (timestamp, results)


def api_key():
    return (os.environ.get('SCRAPINGBEE_API_KEY') or '').strip()


def is_configured():
    return bool(api_key())


def _today():
    return datetime.datetime.utcnow().strftime('%Y-%m-%d')


def _reserve_request():
    """Count a request against the daily cap BEFORE spending credits."""
    with _lock:
        if _usage['day'] != _today():
            _usage['day'] = _today()
            _usage['count'] = 0
        if _usage['count'] >= MAX_REQ_PER_DAY:
            raise ScholarError(
                f"Daily Google Scholar request cap reached ({MAX_REQ_PER_DAY}). "
                "This protects your ScrapingBee credits; raise SCHOLAR_MAX_REQUESTS_PER_DAY "
                "if you really want more.")
        _usage['count'] += 1
        _usage['total'] += 1


def status():
    with _lock:
        used = _usage['count'] if _usage['day'] == _today() else 0
        return {
            'configured': is_configured(),
            'requests_today': used,
            'daily_cap': MAX_REQ_PER_DAY,
            'requests_left_today': max(0, MAX_REQ_PER_DAY - used),
            'parser_available': BeautifulSoup is not None,
            'last_error': _usage['last_error'],
        }


# ─── Parsing ─────────────────────────────────────────────────────────
_YEAR_RE = re.compile(r'\b(19[5-9]\d|20[0-4]\d)\b')
_CITED_RE = re.compile(r'Cited by\s+([\d,]+)', re.I)
_BLOCK_MARKERS = ('unusual traffic', 'not a robot', 'recaptcha', 'gs_captcha', 'before you continue')


def _clean(text):
    return re.sub(r'\s+', ' ', (text or '')).strip()


def parse_scholar_html(html):
    """HTML of a Scholar results page -> list of normalized dicts."""
    if BeautifulSoup is None:
        raise ScholarError("beautifulsoup4 is not installed. Run: pip install beautifulsoup4")
    soup = BeautifulSoup(html, 'html.parser')
    blocks = soup.select('div.gs_r.gs_or') or soup.select('div.gs_ri')
    if not blocks:
        low = html.lower()
        if any(m in low for m in _BLOCK_MARKERS):
            raise ScholarBlocked("Google Scholar returned a CAPTCHA/consent page instead of results. "
                                 "Try again in a few minutes.")
        return []   # genuinely zero results (or unchanged markup)
    out = []
    for blk in blocks:
        ri = blk if 'gs_ri' in (blk.get('class') or []) else (blk.select_one('div.gs_ri') or blk)
        title_el = ri.select_one('h3.gs_rt') or ri.select_one('.gs_rt')
        if not title_el:
            continue
        for junk in title_el.select('.gs_ctg2, .gs_ct1, .gs_ct2'):
            junk.decompose()                       # "[PDF]", "[BOOK]" tags
        a = title_el.select_one('a')
        title = _clean(title_el.get_text(' '))
        if not title:
            continue
        meta = _clean((ri.select_one('.gs_a') or blk).get_text(' ')) if ri.select_one('.gs_a') else ''
        authors_part, _, rest = meta.partition(' - ')
        venue_part = rest.split(' - ')[0] if rest else ''
        year_m = _YEAR_RE.search(venue_part) or _YEAR_RE.search(meta)
        authors = [x.strip() for x in authors_part.split(',') if x.strip() and x.strip() != '…'][:6]
        venue = _clean(_YEAR_RE.sub('', venue_part).strip(' ,'))
        snippet_el = ri.select_one('.gs_rs')
        snippet = _clean(snippet_el.get_text(' ')) if snippet_el else ''
        cited = None
        for link in ri.select('.gs_fl a'):
            m = _CITED_RE.search(link.get_text(' '))
            if m:
                cited = int(m.group(1).replace(',', ''))
                break
        pdf = ''
        pdf_a = blk.select_one('.gs_or_ggsm a') or blk.select_one('.gs_ggs a')
        if pdf_a and pdf_a.get('href'):
            pdf = pdf_a['href']
        out.append({
            'title': title,
            'authors': authors,
            'year': year_m.group(1) if year_m else 'N/A',
            'venue': venue,
            'snippet': snippet,
            'link': a['href'] if a and a.get('href') else '',
            'pdf_link': pdf,
            'cited_by': cited,
        })
    return out


def _parse_json_shape(data):
    """Tolerate a JSON response shaped like ScrapingBee's README example
    (`organic_results`), in case the account/endpoint returns that."""
    out = []
    for r in (data.get('organic_results') or []):
        cb = r.get('cited_by')
        cited = cb.get('value') if isinstance(cb, dict) else cb
        info = r.get('publication_info') or ''
        ym = _YEAR_RE.search(str(info))
        authors = r.get('authors') or ''
        out.append({
            'title': _clean(r.get('title')),
            'authors': [x.strip() for x in authors.split(',') if x.strip()] if isinstance(authors, str) else list(authors),
            'year': ym.group(1) if ym else 'N/A',
            'venue': _clean(_YEAR_RE.sub('', str(info))),
            'snippet': _clean(r.get('snippet')),
            'link': r.get('link') or '',
            'pdf_link': r.get('pdf_link') or '',
            'cited_by': cited if isinstance(cited, int) else None,
        })
    return [x for x in out if x['title']]


# ─── Fetching ────────────────────────────────────────────────────────
def _scholar_url(query, start=0, year_from=None, year_to=None):
    url = f"{SCHOLAR_URL}?q={quote_plus(query)}&hl=en&as_sdt=0,5"
    if start:
        url += f"&start={int(start)}"
    if year_from:
        url += f"&as_ylo={int(year_from)}"
    if year_to:
        url += f"&as_yhi={int(year_to)}"
    return url


def fetch_page(query, start=0, year_from=None, year_to=None, use_cache=True):
    """One Scholar results page (10 results). Cached; counted against the cap."""
    if not is_configured():
        raise ScholarNotConfigured("SCRAPINGBEE_API_KEY is not set. Add it to backend/.env "
                                   "(free trial key at scrapingbee.com).")
    query = _clean(query)
    if not query:
        raise ScholarError("Empty search query.")
    ck = (query.lower(), int(start), year_from, year_to)
    now = time.time()
    if use_cache:
        hit = _cache.get(ck)
        if hit and now - hit[0] < CACHE_TTL_S:
            return hit[1]

    params = {
        'api_key': api_key(),
        'url': _scholar_url(query, start, year_from, year_to),
        'country_code': 'us',
        'custom_google': 'true',
    }
    last_exc = None
    for attempt in range(3):
        _reserve_request()
        try:
            resp = requests.get(SB_ENDPOINT, params=params, timeout=TIMEOUT_S)
        except requests.RequestException as e:
            last_exc = ScholarError(f"Could not reach ScrapingBee: {e}")
            time.sleep(2 ** attempt)
            continue
        code = resp.status_code
        if code == 200:
            ctype = (resp.headers.get('Content-Type') or '').lower()
            if 'json' in ctype:
                try:
                    results = _parse_json_shape(resp.json())
                except ValueError:
                    results = parse_scholar_html(resp.text)
            else:
                results = parse_scholar_html(resp.text)
            _cache[ck] = (now, results)
            return results
        if code in (401, 403):
            raise ScholarError(f"ScrapingBee rejected the request ({code}). Check SCRAPINGBEE_API_KEY "
                               "and that your plan allows Google requests.")
        if code == 402:
            raise ScholarError("ScrapingBee says you are out of credits (402).")
        if code in (429, 500, 502, 503, 504):
            last_exc = ScholarError(f"ScrapingBee temporary error ({code}).")
            time.sleep(2 ** (attempt + 1))
            continue
        raise ScholarError(f"ScrapingBee returned {code}: {resp.text[:200]}")
    _usage['last_error'] = str(last_exc)
    raise last_exc or ScholarError("Google Scholar request failed.")


def search(query, pages=1, year_from=None, year_to=None):
    """Up to `pages` pages (10 results each), politely spaced."""
    pages = max(1, min(int(pages), 5))
    results, seen = [], set()
    for i in range(pages):
        if i:
            time.sleep(PAGE_DELAY_S)
        page = fetch_page(query, start=i * 10, year_from=year_from, year_to=year_to)
        if not page:
            break
        for r in page:
            key = re.sub(r'[^a-z0-9]+', ' ', r['title'].lower()).strip()
            if key and key not in seen:
                seen.add(key)
                results.append(r)
    return results


# ─── As a paper source (plugs into app.py's multi-source fetch) ──────
def to_paper(r):
    """Scholar record -> the paper dict shape the rest of the app uses."""
    return {
        'title': r['title'],
        'abstract': (r['snippet'] or 'No abstract available.')[:500],
        'abstract_is_snippet': bool(r['snippet']),
        'year': r['year'],
        'authors': r['authors'][:3],
        'categories': ['Academic Paper'],
        'source': 'Google Scholar',
        'doi': '',
        'venue': r['venue'],
        'citation_count': r['cited_by'],
        'url': r['link'],
        'pdf_url': r['pdf_link'],
    }


def fetch_google_scholar(topic, max_results=20, offset=0, **_ignored):
    """Signature-compatible with the other fetch_* sources
    (`fn(topic, max_results)`), but hard-capped in pages so a deep
    "page everything" collector can't burn credits."""
    pages = max(1, min(SOURCE_MAX_PAGES, (max_results + 9) // 10))
    start_page = max(0, int(offset)) // 10
    if start_page >= SOURCE_MAX_PAGES:
        return []        # deep paging past the cap -> stop, don't spend credits
    records = []
    for i in range(pages):
        if start_page + i >= SOURCE_MAX_PAGES:
            break
        if i:
            time.sleep(PAGE_DELAY_S)
        page = fetch_page(topic, start=(start_page + i) * 10)
        if not page:
            break
        records.extend(page)
    return [to_paper(r) for r in records[:max_results]]


# ─── Gap reality check ───────────────────────────────────────────────
_STOP = set('the a an and or of for in on to with by from is are be as at that this these those it its their '
            'using based via into over under between across towards toward than then also can may not no '
            'study approach method methods model models data analysis research paper new novel '
            'lack lacks limited limitation limitations gap gaps framework'.split())


def _terms(text):
    return {w for w in re.findall(r'[a-z][a-z0-9\-]{2,}', (text or '').lower()) if w not in _STOP}


def overlap_score(gap_text, result):
    """Share of the gap's key terms that appear in a result's title+snippet
    (0..1). A rough lexical signal, nothing more."""
    g = _terms(gap_text)
    if not g:
        return 0.0
    r = _terms(result.get('title', '') + ' ' + result.get('snippet', ''))
    return round(len(g & r) / len(g), 3)


def check_gap(gap_title, description='', year_from=None, top_n=5, close_threshold=0.5):
    """Search Scholar for a gap's wording; return the closest existing work.

    `verdict`:
      'close_matches_found' -> at least one result shares >= close_threshold
                               of the gap's key terms: READ THEM, the gap may
                               already be addressed.
      'no_close_matches'    -> nothing close among the top results. That does
                               NOT prove the gap is real; Scholar search is
                               keyword-based and only the first page is read.
    """
    if not _clean(gap_title):
        raise ScholarError("gap_title is required.")
    results = fetch_page(gap_title, year_from=year_from)
    gap_text = f"{gap_title} {description}"
    scored = []
    for r in results:
        item = dict(r)
        item['overlap'] = overlap_score(gap_text, r)
        scored.append(item)
    scored.sort(key=lambda x: (x['overlap'], x.get('cited_by') or 0), reverse=True)
    top = scored[:top_n]
    close = [x for x in top if x['overlap'] >= close_threshold]
    return {
        'query': gap_title,
        'results_read': len(results),
        'verdict': 'close_matches_found' if close else 'no_close_matches',
        'matches': top,
        'note': ('A text-overlap signal over the first page of Google Scholar results. '
                 'It cannot prove a gap is real or already solved -- read the matches.'),
    }
