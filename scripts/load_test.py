"""Small dependency-free load test (stdlib only).

    python scripts/load_test.py http://127.0.0.1:8000 --paths /health /ready --concurrency 1 8 32 --requests 400

For each concurrency level it fires `--requests` GETs per path from that many threads and prints
throughput, error rate and p50/p95/p99 latency. Exits non-zero if any run breaches --max-error-rate or
--max-p95-ms, so it can gate CI.

What it measures: the web tier of ONE running instance on the machine you run it from. It does not
measure the analysis pipeline (that is bounded by Groq/OpenAlex rate limits, not by this server).
"""
import argparse
import json
import sys
import threading
import time
import urllib.request


def _percentile(sorted_ms, q):
    if not sorted_ms:
        return 0.0
    return sorted_ms[min(len(sorted_ms) - 1, int(round(q * (len(sorted_ms) - 1))))]


def run_level(base, path, concurrency, total, timeout):
    per_thread = max(1, total // concurrency)
    latencies, errors, lock = [], [], threading.Lock()

    def worker():
        for _ in range(per_thread):
            t0 = time.perf_counter()
            try:
                with urllib.request.urlopen(base.rstrip('/') + path, timeout=timeout) as r:
                    r.read()
                    ok = 200 <= r.status < 300
            except Exception as e:                                   # noqa: BLE001 - count every failure
                ok = False
                with lock:
                    errors.append(type(e).__name__)
            dt = (time.perf_counter() - t0) * 1000
            with lock:
                latencies.append(dt)
                if not ok and not errors:
                    errors.append('non-2xx')

    threads = [threading.Thread(target=worker) for _ in range(concurrency)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - t0
    lat = sorted(latencies)
    n = len(lat)
    return {'path': path, 'concurrency': concurrency, 'requests': n, 'errors': len(errors),
            'error_rate': len(errors) / n if n else 1.0, 'rps': n / wall if wall else 0.0,
            'p50_ms': _percentile(lat, 0.50), 'p95_ms': _percentile(lat, 0.95), 'p99_ms': _percentile(lat, 0.99)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('base')
    ap.add_argument('--paths', nargs='+', default=['/health'])
    ap.add_argument('--concurrency', nargs='+', type=int, default=[1, 8, 32])
    ap.add_argument('--requests', type=int, default=400)
    ap.add_argument('--timeout', type=float, default=10)
    ap.add_argument('--max-error-rate', type=float, default=0.01)
    ap.add_argument('--max-p95-ms', type=float, default=1000)
    ap.add_argument('--json', help='write results to this file')
    a = ap.parse_args(argv)
    results, failed = [], False
    print(f"{'path':10}{'conc':>6}{'reqs':>7}{'err%':>7}{'rps':>8}{'p50ms':>8}{'p95ms':>8}{'p99ms':>8}")
    for path in a.paths:
        for c in a.concurrency:
            r = run_level(a.base, path, c, a.requests, a.timeout)
            results.append(r)
            print(f"{r['path']:10}{r['concurrency']:>6}{r['requests']:>7}{r['error_rate']*100:>6.1f}%"
                  f"{r['rps']:>8.0f}{r['p50_ms']:>8.1f}{r['p95_ms']:>8.1f}{r['p99_ms']:>8.1f}")
            if r['error_rate'] > a.max_error_rate or r['p95_ms'] > a.max_p95_ms:
                failed = True
    if a.json:
        with open(a.json, 'w') as f:
            json.dump(results, f, indent=2)
    print('FAIL: thresholds breached' if failed else 'PASS: within thresholds')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
