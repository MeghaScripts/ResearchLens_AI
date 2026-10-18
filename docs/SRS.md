# Software Requirements Specification — ResearchLens AI

Version 1.0 · Process: see [SDLC_AGILE.md](SDLC_AGILE.md) · Author: Megha M · Based on the implemented system (IEEE 830 style)

## 1. Introduction
### 1.1 Purpose
ResearchLens AI helps researchers find **research gaps**: topics the literature has not yet covered. It collects papers from scholarly sources, verifies their quality, analyses them with an LLM, and presents gaps, trends, novelty checks and planning aids.

### 1.2 Scope
A web application (Flask backend, HTML/JS frontend) used by students and researchers. It does not replace expert judgement; every gap is a suggestion backed by cited papers.

### 1.3 Definitions
| Term | Meaning |
|---|---|
| Research gap | A question or area the collected literature leaves unaddressed |
| Standard analysis | Single-topic analysis (optional PDF) using up to ~500 papers |
| Deep research | Multi-stage, full-text and trend-based analysis |
| Verification | Checking that a paper is real, relevant, non-retracted and of adequate quality |
| Quality gate | Instant offline filter that removes junk / non-research records |
| Workspace | Shared team space with a join code |

### 1.4 References
Groq API (LLM), Crossref / OpenAlex / arXiv / Semantic Scholar APIs, Firebase Authentication.

## 2. Overall description
### 2.1 Product perspective
Browser client → Flask REST API → SQLite database; external services: Groq, scholarly APIs, Firebase Auth, SMTP (alerts).

### 2.2 User classes
| User | Needs |
|---|---|
| Student / researcher | Find gaps for a topic or paper, export reports |
| Team member | Share papers and gaps, assign and comment |
| Administrator | View usage, manage data (`admin.html`) |

### 2.3 Operating environment
Python 3.11, Flask; modern browser; Windows/Linux; hosted on Render (gunicorn).

### 2.4 Constraints and assumptions
- Users supply their own Groq API key (free tier rate limits apply: requests and tokens per minute/day).
- Scholarly APIs may rate-limit (HTTP 429); the system must degrade gracefully.
- Internet access is required.

## 3. Functional requirements
Priority: H = high, M = medium, L = low.

### 3.1 Authentication and accounts
| ID | Requirement | Pri |
|---|---|---|
| FR-1 | Users sign in with Firebase; every API call except public pages requires a valid token | H |
| FR-2 | Each user can only access their own analyses, runs, chats and notes | H |

### 3.2 Paper collection
| ID | Requirement | Pri |
|---|---|---|
| FR-3 | Search Crossref, OpenAlex, arXiv (and Semantic Scholar) for a topic and merge/deduplicate results | H |
| FR-4 | Let the user choose publication scope (domain, publishers, societies, venues) | M |
| FR-5 | Accept an optional PDF (or several) as the user's own paper | M |

### 3.3 Quality and verification
| ID | Requirement | Pri |
|---|---|---|
| FR-6 | Before analysis, reject junk records: gibberish titles, placeholders, acknowledgment/editorial items, papers with no metadata, papers unrelated to the topic, duplicate titles | H |
| FR-7 | Verify remaining papers (authenticity, relevance, quality, retraction) and assign a status: HIGHLY_VERIFIED, VERIFIED, PARTIALLY_VERIFIED, UNVERIFIED, OFF_TOPIC, REJECTED, RETRACTED | H |
| FR-8 | Exclude REJECTED, RETRACTED and OFF_TOPIC papers from gap analysis; show the rejected list with reasons | H |
| FR-9 | Store verification runs and per-paper results; allow browsing history on `/verify` | M |

### 3.4 Gap analysis
| ID | Requirement | Pri |
|---|---|---|
| FR-10 | Detect research gaps with title, severity, confidence, description, evidence papers and suggested approach | H |
| FR-11 | Scale to hundreds of papers by grouping them into themed batches; merge near-duplicate gaps; cap and rank by severity/confidence | H |
| FR-12 | Show live progress (steps and the papers being analysed) while analysis runs | M |
| FR-13 | Provide keywords, knowledge graph, publication-year and category trends | M |
| FR-14 | Deep research mode with speed presets and source selection, full-text analysis and trend forecasting | M |

### 3.5 Outputs and assistants
| ID | Requirement | Pri |
|---|---|---|
| FR-15 | Generate a PDF report (readable Unicode text, no missing-glyph boxes) | H |
| FR-16 | Export references as BibTeX; export a gap brief as PDF | L |
| FR-17 | Novelty check, research proposal, experiment plan, impact estimate, explanations, thesis check, related work, funding and literature-review helpers per gap | M |
| FR-18 | RAG research assistant answers questions about an analysis | M |

### 3.6 Research Chatbot
| ID | Requirement | Pri |
|---|---|---|
| FR-19 | Upload PDF/TXT/MD papers (≤25 MB), extract, chunk and index them | H |
| FR-20 | Answer questions grounded in the paper with clickable citations; say when the paper does not contain the answer | H |
| FR-21 | Stream answers and allow stopping; keep conversations (rename, pin, delete, search, export to Markdown) | M |
| FR-22 | Generate summaries, exam notes, flashcards, quizzes, mind maps and flowcharts; compare several papers | M |

### 3.7 Collaboration and monitoring
| ID | Requirement | Pri |
|---|---|---|
| FR-23 | Create/join workspaces by code; contribute papers (junk filtered); view library and dashboard | M |
| FR-24 | Assign gaps to members and comment on them | M |
| FR-25 | Share gaps publicly and upvote them (community board) | L |
| FR-26 | Watch a topic and email an alert when new papers/gaps appear (scheduled check) | L |
| FR-27 | Record user feedback on gaps and report accuracy | L |

## 4. External interface requirements
- **UI:** pages `index.html` (analysis), `verify.html`, `deep-research.html`, `explorer.html`, `chatbot.html`, `login.html`, `admin.html`; dark theme; usable on mobile widths.
- **API:** JSON over HTTPS, `Authorization: Bearer <Firebase ID token>`; main routes listed in DESIGN.md §6.
- **External services:** Groq chat completions; Crossref, OpenAlex, arXiv, Semantic Scholar; Firebase Auth; SMTP.

## 5. Non-functional requirements
| ID | Category | Requirement |
|---|---|---|
| NFR-1 | Performance | Quality gate for 500 papers < 1 s; progress visible within seconds of starting |
| NFR-2 | Reliability | A failing external source or rate limit (429) must not abort the run; the quality gate never leaves zero papers |
| NFR-3 | Security | Server-side auth on each endpoint; per-user data isolation; upload type/size checks; sanitized Markdown; secrets only via environment; Groq key not stored by the chatbot |
| NFR-4 | Usability | Clear states (uploading, indexing, ready, failed); honest "not in the paper" answers |
| NFR-5 | Maintainability | Agent-per-task design; automated tests; configuration through environment variables |
| NFR-6 | Portability | Runs locally and on Render with `requirements.txt` |
| NFR-7 | Accuracy | Gaps cite evidence papers; unverifiable quotes are removed |
| NFR-8 | Rate limiting | Chatbot limited to 30 requests/min/user (configurable) |

## 6. Configuration (environment variables)
`GROQ_MODEL`, `GROQ_TPM_BUDGET`, `STANDARD_VERIFY_NETWORK_TOP_N`, `STANDARD_VERIFY_TIMEOUT_S`, `MAX_GAPS`, `GAP_BATCH_TARGET`, `GAP_MAX_BATCHES`, `GAP_DUP_SIMILARITY`, `QUALITY_REQUIRE_TOPIC_OVERLAP`, `CHATBOT_RATE_PER_MIN`, Firebase and SMTP settings.

## 7. Known limitations
- Retrieval in the chatbot is TF-IDF, not embeddings; no OCR for scanned PDFs.
- Results depend on source coverage and LLM quality; gaps need expert review.
- The local quality gate is heuristic and may reject unusual but real titles (can be tuned or disabled for the topic-overlap rule).
