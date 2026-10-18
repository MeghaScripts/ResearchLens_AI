"""Health, readiness, version, metrics and request-id behaviour. Run: python -m pytest tests/test_ops.py -q"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('APP_SECRET_KEY', 'test-only-secret')
os.environ.setdefault('DB_PATH', os.path.join(tempfile.mkdtemp(), 't.db'))
import app as appmod
import monitoring


def client():
    return appmod.app.test_client()


def test_health_is_public_and_reports_version():
    r = client().get('/health')
    assert r.status_code == 200 and r.json['status'] == 'ok' and r.json['version'] == monitoring.read_version()


def test_ready_checks_database(monkeypatch):
    c = client()
    assert c.get('/ready').json['checks']['database'] == 'ok'


def test_version_endpoint():
    assert client().get('/api/version').json['version'] != 'unknown'


def test_request_id_header_is_set_and_echoed():
    c = client()
    assert c.get('/health').headers['X-Request-ID']
    assert c.get('/health', headers={'X-Request-ID': 'abc123'}).headers['X-Request-ID'] == 'abc123'


def test_metrics_counts_requests_and_can_be_protected(monkeypatch):
    c = client()
    monitoring.reset_metrics()
    c.get('/health'); c.get('/health')
    body = c.get('/metrics').get_data(as_text=True)
    assert 'researchlens_http_requests_total{method="GET",route="/health",status="2xx"} 2' in body
    assert 'researchlens_uptime_seconds' in body
    monkeypatch.setenv('METRICS_TOKEN', 's3')
    assert c.get('/metrics').status_code == 403
    assert c.get('/metrics?token=s3').status_code == 200


def test_version_file_is_semver():
    import re
    assert re.fullmatch(r'\d+\.\d+\.\d+', monitoring.read_version())
