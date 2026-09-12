# Agentic AI Architecture

The backend is now organized as an explicit multi-agent pipeline instead
of one long function body. This adds no new dependencies — the agent
layer wraps the existing search/PDF/graph/Groq logic (dependency
injection), plus six new agents for the enterprise-level features.

```
backend/agents/
├── base.py          BaseAgent, AgentResult, trace_to_dicts
├── orchestrator.py  Orchestrator — registers agents, runs them in order,
│                    builds a shared `context` dict + execution trace
├── textsim.py        dependency-free TF-IDF + cosine similarity
├── core_agents.py    wraps existing app.py functions as agents
└── new_agents.py     the 6 new agents (Groq-backed)
```

## Registered agents (`GET /api/agents/list`)

| Agent | Wraps / does |
|---|---|
| `search_agent` | arXiv + Semantic Scholar + OpenAlex, deduped |
| `pdf_agent` / `multi_pdf_agent` | PDF text extraction (single / multi upload) |
| `keyword_agent` | frequency-based key concept extraction |
| `knowledge_graph_agent` | networkx graph + PageRank + community detection |
| `gap_detection_agent` | Groq LLM gap analysis (single & multi-paper prompts) |
| `trend_agent` | publication-year/category trends + emerging trends |
| `citation_agent` | BibTeX export |
| `report_agent` | PDF report generation |
| `monitoring_agent` | watched-topic re-check + diff |
| `rag_assistant_agent` | **new** — Q&A over a saved paper via TF-IDF retrieval + Groq |
| `novelty_agent` | **new** — novelty score vs. fetched literature (TF-IDF similarity + LLM reasoning) |
| `proposal_agent` | **new** — structured research proposal from a chosen gap |
| `experiment_agent` | **new** — datasets / metrics / baselines for a gap |
| `impact_agent` | **new** — publication-trend-based impact score + LLM reasoning |
| `explainability_agent` | **new** — plain-language "why was this surfaced" |

## What changed in existing routes

`/api/analyze` and `/api/analyze-multi` now run as an explicit
`orchestrator.run_agent(...)` pipeline (PDF → Search → Keywords → Gap
Detection → Knowledge Graph → Trends → Report) instead of inline calls.
Behavior and error responses (status codes, `error_type` for
`arxiv_unreachable`, malformed-JSON handling) are unchanged — this is a
structural refactor, not a behavior change. Both now also return:

- `analysis_id` — the saved DB row id, needed by the new `/api/agents/*` routes
- `agent_trace` — `[{agent, summary, success}, ...]` for every step that ran

A `document_text` column was added to the `analyses` table (auto-migrated
on startup, same pattern as the existing `owner_uid` backfill) so the RAG
agent can answer follow-up questions without re-uploading the PDF.

## New endpoints (all `POST`, JSON body, `Authorization: Bearer <token>`)

All six require `analysis_id` (from a prior `/api/analyze[-multi]` call)
and load the saved topic/papers/document text — no re-fetching or
re-uploading needed.

- `POST /api/agents/ask` — `{api_key, question, analysis_id}` → `{answer, retrieved_chunks}`
- `POST /api/agents/novelty` — `{idea, analysis_id, api_key?}` → `{novelty_score, closest_matches, reasoning}`
- `POST /api/agents/proposal` — `{api_key, analysis_id, gap_index}` → `{proposal: {...}}`
- `POST /api/agents/experiment` — `{api_key, analysis_id, gap_index}` → `{experiment_plan: {...}}`
- `POST /api/agents/impact` — `{analysis_id, api_key?}` → `{impact_score, reasoning, ...}`
- `POST /api/agents/explain` — `{analysis_id, gap_index, api_key?}` → `{narrative, pipeline_trace}`

`api_key?` marked routes work without a Groq key (heuristic-only output);
supplying one adds LLM reasoning on top.

## Not done in this pass (frontend)

`index.html` / `app.js` were not touched — the new `/api/agents/*`
endpoints are backend-only for now. Say the word if you want UI added
(e.g. a "Q&A", "Check novelty", "Generate proposal" panel per gap card).

---

# Enterprise layer (second pass)

Five more agents, a thread-pool-based parallel execution path, a
minimal event bus, and SQLite-backed memory/vector-store modules were
added on top of the 16 above. Scope note up front: this app is a
single Flask process with a handful of concurrent users, so "enterprise"
here means *right-sized for that*, not a microservices rewrite —
see the inline docstrings in each new file for why each one stops
where it does (e.g. `memory/vector_store.py` is TF-IDF + SQLite, not
FAISS; `events/event_bus.py` is an in-process dict of callbacks, not
Redis/RabbitMQ).

## New agents (`backend/agents/enterprise_agents.py`)

| Agent | Wraps / does |
|---|---|
| `metadata_agent` | Extracts DOI, authors, venue, year into a structured record per paper (Semantic Scholar/OpenAlex fetches were extended to actually request/expose `doi`/`venue`, which existed in the APIs but weren't being surfaced before) |
| `embedding_agent` | Computes TF-IDF vectors via `memory/vector_store.py` and persists them, keyed by collection |
| `validation_agent` | Checks a completed analysis for empty gap sections, missing emerging trends, duplicate paper titles, and zero fetched papers — runs right before `report_agent` |
| `planner_agent` | Static task-name → required-agent-list lookup (`_TASK_PLANS` in the same file) — makes explicit what was already true implicitly (each `/api/agents/*` route only ever calls the one agent it needs) |
| `memory_agent` | Thin `BaseAgent` wrapper around `ContextMemory` (get/set/append_conversation/list_keys) so memory operations show up in the agent trace like everything else |

## Parallel execution

`Orchestrator.run_parallel(agent_names, context)` (in `orchestrator.py`)
runs independent agents concurrently on a `ThreadPoolExecutor`. Used in
two places:

1. **`fetch_papers_multi_source`** (in `app.py`) — the three paper-API
   calls (arXiv, Semantic Scholar, OpenAlex) now fire concurrently
   instead of sequentially. This is the real win: three blocking HTTP
   calls to independent hosts is exactly the case where Python threads
   help despite the GIL (it's released during I/O wait).
2. **`/api/analyze` and `/api/analyze-multi`** — `keyword_agent` +
   `metadata_agent` run in parallel after search (both only need
   `context['papers']`), and `knowledge_graph_agent` + `trend_agent` run
   in parallel after gap detection (neither depends on the other's
   output). These are CPU-bound, so the win is smaller than the network
   case above, but they're genuinely independent so there's no
   correctness cost to running them concurrently.

## Event bus (`backend/events/`)

`EventBus.publish()` is called from inside `Orchestrator._record()` on
every agent completion (`agent.<name>.completed`). One default
subscriber is registered at startup (`subscribers.py`) that prints an
audit line. This is the seam for adding real subscribers later
(metrics, per-user notifications) without editing the orchestrator or
any agent.

## Memory (`backend/memory/context_memory.py`)

One new table, `agent_memory`, scoped by `(owner_uid, analysis_id,
kind, key)` with `kind` restricted to `conversation` / `research` /
`gap` / `proposal`. `POST /api/agents/ask` now writes each Q&A turn to
`conversation` memory automatically; `GET
/api/agents/memory/<analysis_id>` returns the stored history. Nothing
yet reads this memory back into a prompt (i.e. there's no
multi-turn-aware RAG agent yet) — the storage and retrieval endpoints
exist, but wiring "continue from the previous answer" into
`rag_assistant_agent`'s prompt is a follow-up, not done in this pass.

## Vector store (`backend/memory/vector_store.py`)

One new table, `embeddings` (`collection`, `item_id`, `vector_json`).
`/api/analyze` now chunks the uploaded PDF and embeds it under a
temporary collection, re-keyed to `analysis_{id}` once the DB row
exists. Nothing currently reads from this store at request time — the
existing `rag_assistant_agent` still does its own TF-IDF `most_similar`
call directly against `document_text` per question, which is simpler
and works fine at this scale. The vector store is there so that could
be swapped for a persisted-embeddings lookup without recomputing TF-IDF
per question, if/when that becomes worth doing.

## New endpoints

- `POST /api/agents/plan` — `{task}` → `{task, plan: [agent names]}`. Known tasks: `full_analysis`, `ask_question`, `check_novelty`, `generate_proposal`, `design_experiment`, `estimate_impact`, `explain_finding`, `monitor_topic`.
- `GET /api/agents/memory/<analysis_id>` — returns stored conversation history for that analysis.

`/api/analyze` and `/api/analyze-multi` responses now also include
`metadata` (from `metadata_agent`) and `validation` (issues found by
`validation_agent`, non-blocking — a report still generates even with
validation warnings, they're surfaced for visibility, not enforced).

## What's genuinely NOT here

Being upfront about the parts of the original enterprise write-up that
were **not** implemented, and why:

- **No real vector DB / embedding model.** TF-IDF + SQLite, as above. A
  real embedding model (sentence-transformers) would need a new
  dependency and meaningfully more deploy weight for a corpus size
  (one paper, ~50 abstracts) where it wouldn't change search quality
  enough to justify that.
- **No dynamic/LLM-driven planning.** `planner_agent` is a static
  lookup table, not a model call deciding the plan — the app already
  knows which agent a given route needs; asking an LLM to re-derive
  that at request time would only add latency and cost.
- **Tool abstraction layer / multi-provider fallback** (e.g. CrossRef
  as a fourth paper source, swappable per-agent tool objects) — not
  added. The three-source fallback logic already lives in
  `fetch_papers_multi_source`; formalizing it into a `tools/` package
  is a real refactor, not a small addition, and doesn't change
  behavior on its own.
- **Full plugin/auto-discovery agent registry** — agents are still
  registered explicitly in `app.py` (now 21 lines instead of 16).
  Auto-discovery from a directory saves ~5 lines of code at the cost of
  making it harder to see what's actually running; not worth it yet.
