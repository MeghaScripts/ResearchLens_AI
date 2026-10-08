"""
http_guard.py - fail-fast fallback for rate-limited academic APIs.

After a host answers 429 (or 503 with Retry-After), further calls to that host
are short-circuited for a cooldown, so verification falls back quickly
instead of retrying per paper. Optional API keys are attached if set:
    OPENALEX_API_KEY   -> sent as ?api_key=...
    S2_API_KEY         -> sent as header x-api-key (Semantic Scholar)
Tunables (env): GUARD_COOLDOWN_SECONDS (default 120), GUARD_MAX_COOLDOWN (600)
"""
import os, threading, time
from urllib.parse import urlparse
import requests

_DEFAULT_COOLDOWN = float(os.environ.get('GUARD_COOLDOWN_SECONDS', '120'))
_MAX_COOLDOWN = float(os.environ.get('GUARD_MAX_COOLDOWN', '600'))
GUARDED_HOSTS = {'api.openalex.org', 'api.semanticscholar.org'}

_lock = threading.Lock()
_blocked_until = {}


class HostCoolingDown(requests.exceptions.RequestException):
    pass


def _host(url):
    try:
        return (urlparse(url).hostname or '').lower()
    except Exception:
        return ''


_real_get = requests.get


def guarded_get(url, *args, **kwargs):
    host = _host(url)
    if host not in GUARDED_HOSTS:
        return _real_get(url, *args, **kwargs)

    with _lock:
        remaining = _blocked_until.get(host, 0) - time.time()
    if remaining > 0:
        raise HostCoolingDown(f"{host} rate-limited; skipped (cooldown {int(remaining)}s left)")

    if host == 'api.openalex.org' and os.environ.get('OPENALEX_API_KEY'):
        params = dict(kwargs.get('params') or {})
        params.setdefault('api_key', os.environ['OPENALEX_API_KEY'])
        kwargs['params'] = params
    if host == 'api.semanticscholar.org' and os.environ.get('S2_API_KEY'):
        headers = dict(kwargs.get('headers') or {})
        headers.setdefault('x-api-key', os.environ['S2_API_KEY'])
        kwargs['headers'] = headers
    kwargs.setdefault('timeout', 10)

    resp = _real_get(url, *args, **kwargs)

    if resp.status_code == 429 or (resp.status_code == 503 and resp.headers.get('Retry-After')):
        wait = _DEFAULT_COOLDOWN
        ra = resp.headers.get('Retry-After', '')
        if ra.isdigit():
            wait = float(ra)
        wait = min(max(wait, 5.0), _MAX_COOLDOWN)
        with _lock:
            already = _blocked_until.get(host, 0) > time.time()
            _blocked_until[host] = time.time() + wait
        if not already:
            print(f"[http_guard] {host} returned {resp.status_code} - skipping it for {int(wait)}s "
                  f"(falling back to other sources)")
    return resp


if requests.get is not guarded_get:
    requests.get = guarded_get
    try:
        requests.api.get = guarded_get
    except Exception:
        pass