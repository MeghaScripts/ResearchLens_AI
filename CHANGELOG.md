# Changelog
All notable changes. Format: [Keep a Changelog](https://keepachangelog.com), versioning: [SemVer](https://semver.org).

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
