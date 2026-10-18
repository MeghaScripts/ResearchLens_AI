"""
openalex_client.py — one place that makes every call to api.openalex.org behave.

Why this exists
---------------
Since Feb 2026 OpenAlex meters usage against a DAILY budget.
  * no API key      -> tiny budget (about $0.10/day)  -> 429 very quickly
  * free API key    -> about $1/day (10x more)        -> get one at
                       https://openalex.org/settings/api
A fake `mailto=research@example.com` does nothing, so this module strips it
and only sends a mailto if you configure a real one.

What install_everywhere() does
------------------------------
It wraps requests' Session.send ONCE, so every existing
`requests.get("https://api.openalex.org/...")` in the project (app.py,
academic_sources.py, venue_registry.py, paper_verification.py,
deep_collection.py ...) automatically gets:
  1. api_key injected (from OPENALEX_API_KEY)      - placeholder mailto removed
  2. a polite throttle between requests
  3. retry with backoff on 429 / 5xx (honours Retry-After)
  4. a short in-memory cache for identical GETs (repeat searches are free)
  5. a circuit breaker: once the daily budget is clearly spent, further calls
     fail fast for a few minutes instead of hammering the API
Callers still get a normal requests.Response, so their existing
`raise_for_status()` / error handling keeps working unchanged.

Config (environment variables, or a backend/.env file)
------------------------------------------------------
  OPENALEX_API_KEY   your free key (strongly recommended)
  OPENALEX_MAILTO    a real contact email (optional)
"""
import os
import re
import time
import threading
from collections import OrderedDict
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import requests
from requests.structures import CaseInsensitiveDict

_HOST = "api.openalex.org"
_PLACEHOLDER_MAILTO = re.compile(r"@example\.(com|org|net)$", re.I)

MIN_INTERVAL = 0.25        # seconds between requests (about 4/sec, well under limits)
MAX_RETRIES = 3            # retries on 429 / 5xx
MAX_SLEEP = 20.0           # never sleep longer than this inside one retry
CACHE_TTL = 6 * 3600       # seconds
CACHE_MAX = 500            # entries
BREAKER_SECONDS = 300      # fail fast this long after the budget is spent

_lock = threading.Lock()
_last_call = 0.0
_cache = OrderedDict()
_breaker_until = 0.0
_installed = False
_orig_send = None


# ── config ────────────────────────────────────────────────────────────
def _load_dotenv_once():
    """Tiny .env reader so OPENALEX_API_KEY works even without python-dotenv."""
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.join(here, ".env"), os.path.join(os.path.dirname(here), ".env")):
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k and k not in os.environ:
                        os.environ[k] = v
        except OSError:
            pass


def get_api_key():
    return (os.environ.get("OPENALEX_API_KEY") or "").strip()


def get_mailto():
    m = (os.environ.get("OPENALEX_MAILTO") or "").strip()
    return "" if (not m or _PLACEHOLDER_MAILTO.search(m)) else m


def status():
    """Handy for debugging: python -c "import openalex_client as o; print(o.status())" """
    return {
        "installed": _installed,
        "api_key_set": bool(get_api_key()),
        "mailto": get_mailto() or None,
        "cache_entries": len(_cache),
        "breaker_seconds_left": max(0, int(_breaker_until - time.time())),
    }


# ── helpers ───────────────────────────────────────────────────────────
def _is_openalex(url):
    try:
        return urlsplit(url).hostname == _HOST
    except Exception:
        return False


def _rewrite_url(url):
    """Inject api_key, drop placeholder mailto. Returns (new_url, cache_key)."""
    parts = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    q = [(k, v) for k, v in q if k not in ("api_key", "mailto")]
    cache_key = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), ""))
    key, mail = get_api_key(), get_mailto()
    if mail:
        q.append(("mailto", mail))
    if key:
        q.append(("api_key", key))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), "")), cache_key


def _throttle():
    global _last_call
    with _lock:
        wait = MIN_INTERVAL - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()


def _retry_after(resp, attempt):
    ra = resp.headers.get("Retry-After")
    if ra:
        try:
            return float(ra)
        except ValueError:
            pass
    return float(2 ** attempt)          # 1s, 2s, 4s ...


def _budget_spent(resp):
    if resp.headers.get("X-RateLimit-Remaining") == "0":
        return True
    ra = resp.headers.get("Retry-After")
    try:
        return ra is not None and float(ra) > MAX_SLEEP
    except ValueError:
        return False


def _clone(cached, request):
    r = requests.Response()
    r.status_code = cached["status"]
    r._content = cached["content"]
    r.headers = CaseInsensitiveDict(cached["headers"])
    r.encoding = cached["encoding"]
    r.url = request.url
    r.request = request
    r.reason = "OK (cached)"
    return r


def _fake_429(request, why):
    r = requests.Response()
    r.status_code = 429
    r._content = ('{"error":"%s"}' % why).encode()
    r.headers = CaseInsensitiveDict({"Content-Type": "application/json"})
    r.url = request.url
    r.request = request
    r.reason = "Too Many Requests"
    return r


# ── the patched send ──────────────────────────────────────────────────
def _patched_send(session, request, **kwargs):
    global _breaker_until
    if not _is_openalex(request.url):
        return _orig_send(session, request, **kwargs)

    new_url, cache_key = _rewrite_url(request.url)
    request.url = new_url
    cacheable = request.method == "GET"

    if cacheable:
        with _lock:
            hit = _cache.get(cache_key)
            if hit and time.time() - hit["t"] < CACHE_TTL:
                _cache.move_to_end(cache_key)
                return _clone(hit, request)

    if time.time() < _breaker_until:
        return _fake_429(request, "OpenAlex daily budget spent - add OPENALEX_API_KEY or retry later")

    resp = None
    for attempt in range(MAX_RETRIES + 1):
        _throttle()
        resp = _orig_send(session, request, **kwargs)
        if resp.status_code == 429 or resp.status_code in (502, 503, 504):
            if resp.status_code == 429 and _budget_spent(resp):
                _breaker_until = time.time() + BREAKER_SECONDS
                print("[openalex_client] daily budget spent -> pausing OpenAlex for "
                      f"{BREAKER_SECONDS}s. Set OPENALEX_API_KEY (free) to raise the limit.")
                return resp
            if attempt < MAX_RETRIES:
                time.sleep(min(_retry_after(resp, attempt), MAX_SLEEP))
                continue
        break

    if cacheable and resp is not None and resp.status_code == 200:
        with _lock:
            _cache[cache_key] = {
                "t": time.time(), "status": 200, "content": resp.content,
                "headers": dict(resp.headers), "encoding": resp.encoding,
            }
            while len(_cache) > CACHE_MAX:
                _cache.popitem(last=False)
    return resp


# ── public API ────────────────────────────────────────────────────────
def install_everywhere():
    """Patch requests once so ALL OpenAlex calls in the process are protected."""
    global _installed, _orig_send
    if _installed:
        return
    _load_dotenv_once()
    _orig_send = requests.sessions.Session.send

    def send(self, request, **kwargs):
        return _patched_send(self, request, **kwargs)

    requests.sessions.Session.send = send
    _installed = True
    if get_api_key():
        print("[openalex_client] installed (API key found).")
    else:
        print("[openalex_client] installed - WARNING: no OPENALEX_API_KEY set, "
              "you will hit 429 quickly. Free key: https://openalex.org/settings/api")


def get(url, params=None, **kwargs):
    """Optional direct helper: openalex_client.get(url, params={...}).json()"""
    install_everywhere()
    kwargs.setdefault("timeout", 30)
    return requests.get(url, params=params, **kwargs)