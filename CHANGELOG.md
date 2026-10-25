# Changelog
All notable changes. Format: [Keep a Changelog](https://keepachangelog.com), versioning: [SemVer](https://semver.org).

## [Unreleased]
### Added
- `backend/claim_check.py`: claim-level verification of AI-generated gaps.
  - Gap prompts number the corpus papers `[P1]..[Pn]` and ask for `evidence_ids`; unknown IDs are dropped and recorded in `evidence_ids_unknown`, so a hallucinated paper title can no longer appear as evidence (title matching stays as a fallback).
  - Each gap's cited abstracts are judged `supported / partial / unsupported / contradicted` (`unverifiable` when no abstract exists or the judge is unavailable); the label feeds `priority_score` and is shown on the gap card.
  - Semantic "already addressed" check against recent papers (embeddings, TF-IDF fallback, method is labelled); strengthens `appears_already_addressed`.
  - Optional agreement between runs (`GAP_CONSENSUS_RUNS`, default 1 = off).
- `backend/eval/`: labelled-set builder (OpenAlex), registry cache, 70/30 stratified evaluation with threshold tuning on train only, coverage vs decisive accuracy with Wilson 95% intervals, and an offline smoke test.
- `paper_verification.decision_band()`: accept / reject / review (abstain).
### Fixed
- `verify_papers` no longer aborts when a registry client raises unexpectedly (`_guarded`), and accepts a non-string DOI.
- Daily scheduler now starts on hosts with a non-zoneinfo timezone name (`BackgroundScheduler(timezone='UTC')`).
### Added (per-level evaluation)
- `eval/run_levels.py`: authenticity, relevance and quality measured separately; `eval/relevance_eval.py` (pair-based relevance eval, weight and cutoff fitting), `eval/quality_eval.py` (rating sheet, kappa, Spearman, ridge fit), `eval/fit.py` (pure-Python AUC, Spearman, kappa, logistic and ridge).
- `eval.run_eval --cv K` pooled k-fold evaluation; `--drop-cache`.
- `DEEP_RELEVANCE_WEIGHTS` / `DEEP_QUALITY_WEIGHTS` overrides; optional cross-encoder rerank (`DEEP_CROSS_ENCODER`).
- Semantic Scholar: API key header and 429 back-off; the eval cache no longer stores rate-limited batches and returns copies of cached entries.
### Changed (this round)
- arXiv multi-source fetch retries (https x2, then http) with `ARXIV_TIMEOUT` (default 20 s) instead of one 8 s attempt; the "could not reach any source" message now separates failures from sources skipped for a missing API key.
- Opt-in `DEEP_STRICT_FIELD_REJECT=1` (and `python -m eval.run_eval --strict`): reject on one severe title/author mismatch against the registry record the identifier resolves to. Off by default.
- `eval.run_eval --tune` verifies once and re-applies thresholds instantly.
### Added (reliability)
- `tests/test_reliability.py` fault-injection tests, `scripts/load_test.py`, `docs/SCALABILITY_RELIABILITY.md`.
### Changed
- The proof panel no longer substitutes the first three collected papers when a gap cites none.
### Removed
- `.bak` backups, `diag*.txt` debug dumps and the already-applied `apply_backend_fix.py`.

## [1.0.0] - 2026-10-17
### Added
- Research Chatbot (`/chatbot`): paper upload, cited answers, notes, mind maps, flowcharts, flashcards, quizzes.
- Offline paper quality gate (`paper_quality.py`): removes gibberish, placeholder, acknowledgment/editorial, no-metadata, off-topic and duplicate records before analysis and workspace contribution.
- Theme-based gap batching and similarity de-duplication (up to 40 ranked gaps instead of 15).
- Live progress list refreshed after the quality gate.
- Monitoring: `/health`, `/ready`, `/metrics` (Prometheus), `/api/version`, request IDs, request logging, optional Sentry.
- Docker image, docker-compose, GitHub Actions CI (tests, Docker smoke test, optional Render deploy), tag-based release workflow, Dependabot.
- Documentation: SRS, design, test plan, SDLC/Agile, CI/CD and deployment guide.
### Changed
- OpenAlex requests are throttled and retried on HTTP 429.
- Standard verification checks the top 80 papers and scales its time limit with corpus size.
- PDF reports use bundled DejaVu Sans Condensed (no missing-glyph boxes).
### Fixed
- `test_routes_e2e.py` auth stub accepts `clock_skew_seconds` (all 6 e2e tests pass).
- Tests no longer require optional packages (`numpy`, `pypdf`) and pass in a clean environment.

## How to release
1. Move items from "Unreleased" to a new `## [x.y.z] - date` section here.
2. Put `x.y.z` in `VERSION`, commit and push to `main` (CI must be green).
3. `git tag vx.y.z && git push origin vx.y.z` — the Release workflow tests, builds and pushes `ghcr.io/<owner>/researchlens-ai:vx.y.z`, and creates the GitHub Release with these notes.

## [Unreleased]
