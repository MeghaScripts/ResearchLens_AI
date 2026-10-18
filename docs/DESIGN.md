# Design Document — ResearchLens AI

All diagrams are Mermaid (render on GitHub / VS Code / mermaid.live).

## 1. System architecture
```mermaid
flowchart LR
  subgraph Client[Browser]
    UI[index / verify / deep-research / chatbot / explorer pages]
    FB[Firebase Auth JS]
  end
  subgraph Server[Flask backend - app.py]
    AUTH[require_auth]
    API[REST routes]
    ORCH[Orchestrator + Agents]
    QG[paper_quality.py - quality gate, gap dedupe, batching]
    VER[paper_verification.py]
    CB[chatbot.py]
    PDF[pdf_text.py - report fonts]
    OAC[openalex_client.py - throttle/retry]
    JOBS[jobs.py - live progress]
  end
  DB[(SQLite)]
  subgraph Ext[External services]
    GROQ[Groq LLM]
    SRC[Crossref / OpenAlex / arXiv / Semantic Scholar]
    FA[Firebase Auth]
    SMTP[SMTP]
  end
  UI --> FB --> FA
  UI -->|Bearer token| AUTH --> API
  API --> ORCH --> SRC
  ORCH --> QG --> VER --> OAC --> SRC
  ORCH --> GROQ
  API --> CB --> GROQ
  API --> PDF
  API --> JOBS
  API --> DB
  CB --> DB
  API --> SMTP
```

## 2. Standard analysis pipeline (data flow)
```mermaid
flowchart TD
  A[User: topic + optional PDF + Groq key] --> B[PDF agent]
  B --> C[Search agent: Crossref, OpenAlex, arXiv -> merge + dedupe]
  C --> D{Quality gate - offline}
  D -->|junk| X[LOW_QUALITY list with reasons]
  D -->|kept| E[Online verification - top N, time-boxed]
  E -->|REJECTED / RETRACTED / OFF_TOPIC| R[Rejected list]
  E -->|passed| F[Keyword + Metadata agents]
  F --> G[Domain classification]
  G --> H[Theme batching - TF-IDF clustering]
  H --> I[Gap detection per batch - Groq]
  I --> J[Dedupe by similarity, rank, cap]
  J --> K[Knowledge graph + Trends]
  K --> L[Validation + Embedding agents]
  L --> M[PDF report + DB save]
  M --> N[Result to UI]
```

## 3. Sequence: starting an analysis with live progress
```mermaid
sequenceDiagram
  participant U as Browser
  participant S as Flask API
  participant J as jobs
  participant T as Worker thread
  participant G as Groq
  U->>S: POST /api/analyze/start
  S->>J: create job (uid)
  S->>T: run _run_single_analysis
  S-->>U: job_id
  loop every few seconds
    U->>S: GET /api/analyze/status/job_id
    S-->>U: steps, papers_count, papers_sample
  end
  T->>J: set_papers / add_step (after search and after quality gate)
  T->>G: gap analysis batches
  T->>J: finish_job(result)
  U->>S: status = done
  S-->>U: analysis, papers, rejected_papers, verification
```

## 4. Paper verification state machine
```mermaid
stateDiagram-v2
  [*] --> Collected
  Collected --> LOW_QUALITY: quality gate (junk, off-topic, no metadata, duplicate)
  Collected --> Checking
  Checking --> RETRACTED
  Checking --> REJECTED
  Checking --> OFF_TOPIC
  Checking --> UNVERIFIED
  Checking --> PARTIALLY_VERIFIED
  Checking --> VERIFIED
  Checking --> HIGHLY_VERIFIED
  UNVERIFIED --> Analysed
  PARTIALLY_VERIFIED --> Analysed
  VERIFIED --> Analysed
  HIGHLY_VERIFIED --> Analysed
  LOW_QUALITY --> [*]
  RETRACTED --> [*]
  REJECTED --> [*]
  OFF_TOPIC --> [*]
  Analysed --> [*]
```

## 5. Agents
```mermaid
flowchart TB
  O[Orchestrator]
  O --> core[Core: Search, PDF, MultiPDF, Keyword, KnowledgeGraph, GapDetection, Trend, Citation, Report, Monitoring]
  O --> deep[Deep: DeepCollection, DomainUnderstanding, CurrentTrends, FullTextAnalysis, TrendForecasting, TrendGapDetection, PaperVerification]
  O --> ent[Enterprise: Metadata, Embedding, Validation, Planner, Memory]
  O --> new[Assistants: RAGResearchAssistant, NoveltyVerification, ResearchProposal, ExperimentRecommendation, ResearchImpact, Explainability]
```
Each agent extends `BaseAgent` and returns an `AgentResult` (success, data, error); the orchestrator records a trace per step.

## 6. API overview
| Area | Routes |
|---|---|
| Pages | `/`, `/index.html`, `/login`, `/verify`, `/deep-research`, `/explorer`, `/chatbot` |
| Analysis | `POST /api/analyze`, `/api/analyze/start`, `GET /api/analyze/status/<id>`, `POST /api/analyze-multi`, `/api/deep-research`, `GET /api/deep-research/sources` |
| Verification | `GET /api/verification-runs`, `/api/verification-runs/<id>`, `/api/verification-runs/<id>/papers`, `/api/verification-history` |
| Assistants | `/api/agents/{ask,novelty,proposal,experiment,impact,explain,plan}`, `/api/graph-qa`, `/api/thesis-check`, `/api/related-work`, `/api/gap-funding`, `/api/gap-litreview` |
| Export | `/api/export/bibtex`, `/api/gap-brief/pdf`, `/api/download/<file>` |
| Monitoring | `/api/watch`, `/api/unwatch`, `/api/watched-topics`, `/api/check-now/<id>` |
| Workspaces | `/api/workspace/{create,join}`, `/api/workspace/<code>/{contribute,library,dashboard,gap/assign,gap/assignments,gap/comment,gap/comments}` |
| Community | `/api/community/gaps` (+ share, upvote, delete), `/api/gap-feedback`, `/api/eval/accuracy-summary` |
| Chatbot | `/api/chatbot/*` (documents, conversations, messages, notes, tools) |

## 7. Database (ER diagram)
```mermaid
erDiagram
  analyses ||--o{ gap_feedback : receives
  analyses ||--o{ public_gaps : shared_as
  workspaces ||--o{ workspace_members : has
  workspaces ||--o{ workspace_papers : holds
  workspaces ||--o{ gap_assignments : tracks
  workspaces ||--o{ gap_comments : discusses
  watched_topics ||--o{ topic_snapshots : snapshots
  verification_runs ||--o{ verified_papers : contains
  cb_documents ||--o{ cb_pages : has
  cb_documents ||--o{ cb_chunks : indexed_into
  cb_conversations ||--o{ cb_messages : contains
  cb_documents ||--o{ cb_notes : annotated_by
  domains ||--o{ venue_domains : maps
  venues ||--o{ venue_domains : maps
  domains ||--o{ domain_publishers : maps
  publishers ||--o{ domain_publishers : maps
  domains ||--o{ domain_societies : maps
  societies ||--o{ domain_societies : maps
```
Other tables: `generated_files`, `query_domain_classifications`. Every user-owned table carries an owner/user id and every query filters on it.

## 8. Key design decisions
| Decision | Reason |
|---|---|
| Agent-per-task with an orchestrator | Steps can be run, traced, replaced and run in parallel independently |
| Offline quality gate before network verification | Network checks are partial and time-boxed; junk must never depend on them |
| Theme batching + similarity de-duplication | A single huge prompt yields few gaps; coherent batches yield more distinct, specific gaps |
| SQLite | Zero-setup for a student project; single-file backup |
| Client-supplied Groq key | No shared API cost; key not stored by the chatbot |
| TF-IDF retrieval for chatbot | No extra model or vector service required; fast and explainable |
| Throttled OpenAlex client with 429 retry | Prevents rate-limit floods and keeps verification working |
| Bundled DejaVu Sans Condensed fonts in PDF | Avoids missing-glyph boxes for non-Latin-1 characters |

## 9. Deployment and operations view
```mermaid
flowchart LR
  DEV[Developer] -->|git push| GH[GitHub]
  GH --> CI[Actions: test + docker smoke]
  CI -->|main, deploy hook| R[Render web service: gunicorn]
  GH -->|tag vX.Y.Z| REL[Release workflow] --> GHCR[(ghcr.io image)] --> ANY[Any Docker host]
  R --> HC["/health check"]
  R --> LOGS[Request logs with request id]
  R --> MET["/metrics, Prometheus/Grafana optional"]
  R -.optional.-> SEN[Sentry]
```
Operational endpoints are public except `/metrics`, which can be protected with `METRICS_TOKEN`.

## 10. Security design
Firebase token verification on each request; per-user scoping; upload validation and generated file names; Markdown sanitised with DOMPurify; Mermaid strict mode; rate limiting; secrets in environment variables (`.env` is git-ignored).
