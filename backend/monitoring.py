"""
Monitoring and operations endpoints (no extra dependencies).

  GET /health        liveness  - 200 while the process is up (used by Docker/Render health checks)
  GET /ready         readiness - 200 only if the database is reachable, else 503
  GET /api/version   app version + git commit (VERSION file / APP_COMMIT env)
  GET /metrics       Prometheus text format: request counts, errors, latency sum/count, uptime

Also adds: structured request log lines (method, path, status, ms, request id),
an X-Request-ID response header, logging of unhandled exceptions with a stack trace,
and optional Sentry error tracking when SENTRY_DSN is set (and sentry-sdk is installed).

Environment:
  LOG_LEVEL=INFO|DEBUG|...     SENTRY_DSN=...     METRICS_TOKEN=...  (if set, /metrics needs ?token= or X-Metrics-Token)
"""
import logging
import os
import sqlite3
import threading
import time
import uuid
from collections import defaultdict

from flask import g, jsonify, request, Response

_START = time.time()
_LOCK = threading.Lock()
_COUNT = defaultdict(int)          # (method, route, status_class) -> n
_LAT_SUM = defaultdict(float)      # route -> seconds
_LAT_N = defaultdict(int)          # route -> n
_ERRORS = [0]
_SKIP_LOG = ('/health', '/ready', '/metrics', '/favicon.ico')
log = logging.getLogger('researchlens')


def read_version(root=None):
    root = root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        with open(os.path.join(root, 'VERSION'), encoding='utf-8') as f:
            return f.read().strip() or 'unknown'
    except OSError:
        return 'unknown'


def _setup_logging():
    level = getattr(logging, os.environ.get('LOG_LEVEL', 'INFO').upper(), logging.INFO)
    if not log.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
        log.addHandler(h)
    log.setLevel(level)
    log.propagate = False


def _setup_sentry():
    dsn = os.environ.get('SENTRY_DSN', '').strip()
    if not dsn:
        return False
    try:
        import sentry_sdk
        sentry_sdk.init(dsn=dsn, release=read_version(), traces_sample_rate=0.0,
                        send_default_pii=False)
        log.info('Sentry error tracking enabled')
        return True
    except Exception as e:                      # optional dependency
        log.warning('SENTRY_DSN set but Sentry could not start: %s', e)
        return False


def reset_metrics():
    with _LOCK:
        _COUNT.clear(); _LAT_SUM.clear(); _LAT_N.clear(); _ERRORS[0] = 0


def install(app, db_path_getter=None):
    """Register monitoring on a Flask app. `db_path_getter` returns the SQLite path for /ready."""
    _setup_logging()
    _setup_sentry()
    version = read_version()
    commit = os.environ.get('APP_COMMIT') or os.environ.get('RENDER_GIT_COMMIT') or os.environ.get('GITHUB_SHA') or ''

    @app.before_request
    def _start_timer():
        g._t0 = time.perf_counter()
        g._rid = (request.headers.get('X-Request-ID') or uuid.uuid4().hex[:12])[:64]

    @app.after_request
    def _finish(resp):
        try:
            dt = time.perf_counter() - getattr(g, '_t0', time.perf_counter())
            route = request.url_rule.rule if request.url_rule else 'unmatched'
            with _LOCK:
                _COUNT[(request.method, route, f'{resp.status_code // 100}xx')] += 1
                _LAT_SUM[route] += dt
                _LAT_N[route] += 1
            resp.headers['X-Request-ID'] = getattr(g, '_rid', '')
            if request.path not in _SKIP_LOG and not request.path.startswith('/static'):
                lvl = logging.WARNING if resp.status_code >= 500 else logging.INFO
                log.log(lvl, 'rid=%s %s %s -> %s %.0fms', g._rid, request.method, request.path, resp.status_code, dt * 1000)
        except Exception:                       # monitoring must never break a request
            pass
        return resp

    @app.teardown_request
    def _count_errors(exc):
        if exc is not None:
            with _LOCK:
                _ERRORS[0] += 1
            log.error('rid=%s unhandled exception on %s %s', getattr(g, '_rid', '-'), request.method, request.path, exc_info=exc)

    @app.route('/health')
    def health():
        return jsonify(status='ok', version=version, uptime_s=int(time.time() - _START))

    @app.route('/ready')
    def ready():
        checks = {}
        ok = True
        if db_path_getter:
            try:
                conn = sqlite3.connect(db_path_getter(), timeout=3)
                conn.execute('SELECT 1').fetchone()
                conn.close()
                checks['database'] = 'ok'
            except Exception as e:
                ok = False
                checks['database'] = f'error: {e.__class__.__name__}'
        return jsonify(status='ready' if ok else 'not_ready', checks=checks), (200 if ok else 503)

    @app.route('/api/version')
    def api_version():
        return jsonify(version=version, commit=commit[:12] or None)

    @app.route('/metrics')
    def metrics():
        token = os.environ.get('METRICS_TOKEN', '')
        if token and token not in (request.args.get('token'), request.headers.get('X-Metrics-Token')):
            return jsonify(error='forbidden'), 403
        lines = ['# TYPE researchlens_http_requests_total counter']
        with _LOCK:
            for (m, route, cls), n in sorted(_COUNT.items()):
                lines.append(f'researchlens_http_requests_total{{method="{m}",route="{route}",status="{cls}"}} {n}')
            lines.append('# TYPE researchlens_http_request_seconds_sum counter')
            for route, s in sorted(_LAT_SUM.items()):
                lines.append(f'researchlens_http_request_seconds_sum{{route="{route}"}} {s:.6f}')
                lines.append(f'researchlens_http_request_seconds_count{{route="{route}"}} {_LAT_N[route]}')
            lines.append('# TYPE researchlens_unhandled_exceptions_total counter')
            lines.append(f'researchlens_unhandled_exceptions_total {_ERRORS[0]}')
        lines.append('# TYPE researchlens_uptime_seconds gauge')
        lines.append(f'researchlens_uptime_seconds {int(time.time() - _START)}')
        lines.append(f'researchlens_build_info{{version="{version}"}} 1')
        return Response('\n'.join(lines) + '\n', mimetype='text/plain; version=0.0.4')

    return app
