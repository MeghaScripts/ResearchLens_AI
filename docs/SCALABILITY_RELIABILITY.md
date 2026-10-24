# Scalability and reliability

What was actually tested, what the numbers mean, and where the limits are. Nothing here claims "100%"; each statement
names its evidence.

## Reliability (automated, runs in CI)
`backend/tests/test_reliability.py` injects faults and asserts the pipeline degrades instead of crashing:

| Fault injected | Required behaviour |
|---|---|
| Every registry (Crossref, OpenAlex, Semantic Scholar, DBLP, PubMed, venue, ORCID) raises `ConnectionError` or `TimeoutError` | `verify_papers` completes; every paper gets a verdict; none is ACCEPTED on missing data |
| Malformed records (`None` title, numeric DOI, string authors, empty dict) | no exception |
| Groq quota exhausted / judge returns garbage | claim check keeps every gap, labelled `unverifiable` |
| Junk items in a gap list | de-duplication ignores them |
| 16 threads creating 800 jobs | no lost or duplicate IDs; one user cannot read another's job |

Bugs these tests found and that are now fixed: an unexpected exception from any registry client aborted the whole
verification run (`_guarded` in `paper_verification.py` now turns it into "could not check"); a non-string DOI raised
`AttributeError`; the daily scheduler silently failed to start on hosts whose timezone name `tzlocal` rejects
(now `BackgroundScheduler(timezone='UTC')`).

Existing mechanisms: `/health`, `/ready` (checks the database), `/metrics`, per-host cooldown after HTTP 429, Groq model
fallback chain, partial-result salvage of truncated JSON, Docker `HEALTHCHECK`.

## Load test (web tier, one instance)
`python scripts/load_test.py http://HOST:PORT --paths /health /ready --concurrency 1 8 32 64` (stdlib only, exits non-zero
past `--max-error-rate` / `--max-p95-ms`).

Measured on a 2-core cloud sandbox, `gunicorn --workers 1 --threads 8` (the production command), 640 requests per row:

| Path | Concurrency | Errors | Requests/s | p50 ms | p95 ms | p99 ms |
|---|---|---|---|---|---|---|
| /health | 1 | 0% | 914 | 1.1 | 1.4 | 2.1 |
| /health | 8 | 0% | 1224 | 6.3 | 10.1 | 12.9 |
| /health | 32 | 0% | 1077 | 27.8 | 35.9 | 39.7 |
| /health | 64 | 0% | 1041 | 55.7 | 71.2 | 76.4 |
| /ready | 1 | 0% | 569 | 1.7 | 2.1 | 3.5 |
| /ready | 8 | 0% | 699 | 11.0 | 17.7 | 21.4 |
| /ready | 32 | 0% | 738 | 39.5 | 68.2 | 106.5 |
| /ready | 64 | 0% | 764 | 79.6 | 92.6 | 96.1 |

Scope: these are trivial endpoints on the load generator's own machine, so they show the web tier does not fall over
or leak errors at 64 concurrent clients. They say nothing about the analysis pipeline. Re-run against your deployed URL
(Render free tier will be slower and cold-starts after idle).

## Known limits (state these before an examiner finds them)
- **Analysis throughput is bounded by external APIs**, not this server: Groq free-tier requests/tokens per minute and
  per day, OpenAlex/Crossref politeness limits. One full analysis takes minutes by design (2.5 s pacing between calls).
- **Single process.** Jobs live in an in-memory dict (`jobs.py`), so more than one gunicorn worker or instance would
  break status polling, and a restart loses running jobs. SQLite is a single-file database.
- **Therefore the app is vertically scalable to a point, not horizontally scalable as shipped.**
- Free-tier hosting sleeps when idle and has limited memory (why `sentence-transformers` is excluded from the build).

## Path to horizontal scale (not implemented)
1. Replace `jobs.py` with a shared store/queue (Redis + RQ/Celery); `create_job/add_step/finish_job/get_job` are the seam.
2. Move SQLite to PostgreSQL; keep reports in object storage.
3. Run analysis in workers separate from the web tier; keep web workers stateless behind a load balancer.
4. Share the Groq/registry rate limiter across workers (Redis token bucket).
5. Add an analysis-level load test with recorded API responses to measure pipeline throughput without hitting live APIs.
