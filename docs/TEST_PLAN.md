# Test Plan — ResearchLens AI

## 1. Objectives
Show that the functional (SRS §3) and non-functional (SRS §5) requirements are met, and catch regressions quickly after every change.

## 2. Scope
In scope: backend logic (quality gate, gap de-duplication, batching, verification, chatbot, PDF report, OpenAlex client) and API behaviour. Out of scope for automation: live Groq output quality, real Firebase login, real scholarly API responses (covered by manual testing).

## 3. Test levels and tools
| Level | Tool | Location |
|---|---|---|
| Unit | pytest | `backend/tests/` |
| API / integration | pytest + Flask test client with stubbed auth and fake Groq | `backend/tests/` |
| UI (manual / scripted) | Chromium via Playwright, manual checklist (§6) | – |
| Performance / load | manual timing (§7) | – |

Run: `cd backend && python -m pytest tests -q`

## 4. Automated test suites
| File | Covers | Requirements |
|---|---|---|
| `test_quality_and_gaps.py` | Junk detection (all known junk titles), real titles survive, topic-overlap, duplicates, safety net, gap de-dup/ranking/cap, theme batching on 500 papers, ≥25 gaps from 500 papers with a fake model, gate wired into standard verification, live-progress refresh, timeout scaling | FR-6, FR-8, FR-11, NFR-1, NFR-2 |
| `test_chatbot.py` | Auth required, upload/processing, metadata, upload validation, grounded cited answers, follow-ups, "not in paper", mind-map/flashcard/diagram routing, user isolation, conversations/notes, edit/regenerate, delete, input validation, chunker | FR-19–22, FR-2, NFR-3, NFR-4 |
| `test_openalex_and_pdf.py` | OpenAlex throttle/retry/429 handling, installation into verification module, PDF report text and table rendering with Unicode | FR-7, FR-15, NFR-2 |
| `test_verification_smoke.py` | Verification pipeline smoke | FR-7–9 |
| `test_speed_and_modes.py` | Deep-research speed presets and modes | FR-14 |
| `test_ops.py` | Health, readiness, version, request-id header, metrics and metrics token | NFR-2, NFR-5 |
| `test_routes_e2e.py` | End-to-end routes (standard analysis, deep research, sources) | FR-3, FR-10, FR-14 |

## 5. Manual / functional test cases
| ID | Steps | Expected | Req |
|---|---|---|---|
| TC-01 | Run standard analysis, topic "AI", no PDF | ~500 papers found; junk titles (e.g. "sdfgd", "DGVFD") absent from the live list after the gate step; step "Quality gate removed N…" shown | FR-6, FR-12 |
| TC-02 | Open result | ≥ 15 distinct gaps for a broad topic; none duplicated | FR-10, FR-11 |
| TC-03 | Open rejected papers panel | Gate and verification rejects listed with reasons (LOW_QUALITY, OFF_TOPIC …) | FR-8 |
| TC-04 | Download PDF report | No ■ boxes; severity labels visible | FR-15 |
| TC-05 | Upload PDF in chatbot, ask a question | Cited answer; clicking [S1] opens passage | FR-20 |
| TC-06 | Ask something not in the paper | Model states the paper does not contain it | FR-20 |
| TC-07 | Create workspace, contribute junk + real papers | Junk rejected; library shows only real ones | FR-23 |
| TC-08 | Call API without token | 401 | FR-1 |
| TC-09 | User B requests user A's analysis/chat | Not found / empty | FR-2 |
| TC-10 | Invalid Groq key | Clear error, no crash | NFR-2 |
| TC-11 | Topic with unusual wording, `QUALITY_REQUIRE_TOPIC_OVERLAP=0` | Papers no longer dropped for topic overlap | FR-6 |
| TC-12 | Disconnect network mid-search | Run finishes with available sources or a clear error | NFR-2 |

## 6. UI checklist
Light/dark theme; mobile width without horizontal scroll; stop button during streaming; delete confirmation dialogs; keyboard shortcuts; refresh restores chat.

## 7. Performance checks
| Check | Target |
|---|---|
| Quality gate on 500 papers | < 1 s |
| Theme batching on 500 papers | < 2 s |
| Gap analysis of 500 papers | ≤ 10 Groq calls; total time depends on Groq rate limit (2.5 s pause between calls) |
| Chatbot first token | a few seconds on Groq |

## 8. Entry / exit criteria
- **Entry:** code builds, app boots, dependencies installed.
- **Exit:** all automated tests pass, TC-01–TC-12 pass, no open high-severity defect.

## 9. Environment
Windows 10/11 or Linux, Python 3.11+, Chromium/Edge, `requirements.txt`, test environment variables set by the tests (`APP_SECRET_KEY`, `DB_PATH` temp file).

## 10. Defect handling
Record each defect with steps, expected/actual result and severity (critical / major / minor); fix, add a regression test, re-run the full suite.

## 11. Current status
- Automated: **102 passed, 1 skipped** in a clean environment (the skip is a test of the optional `numpy`/transformer path, which is not installed in CI or Docker).
- `test_routes_e2e.py` is fixed (the auth stub now accepts `clock_skew_seconds`); all 6 e2e tests pass.
- `test_ops.py` covers `/health`, `/ready`, `/api/version`, request IDs and `/metrics`.
- Not automated: real Groq output, real Firebase login, large real PDFs.
- Runs automatically on every push and pull request (`.github/workflows/ci.yml`, Python 3.11 and 3.12), plus a Docker build and `/health` + `/ready` smoke test.

## 12. Risks
| Risk | Mitigation |
|---|---|
| Groq free-tier limits | Batching limits, 2.5 s pacing, configurable `GAP_MAX_BATCHES` |
| Scholarly API 429 / outages | Throttled client, per-source fallbacks |
| Over-strict quality gate rejecting real papers | Safety net, topic-overlap switch, reasons shown to the user |
| LLM invents evidence | Quote verification against the uploaded paper; evidence papers listed |
