"""
Enterprise-layer agents added on top of the original 16: Metadata,
Embedding, Validation, and Planner. Memory is wired in separately (see
memory/context_memory.py) since it's a stateful store rather than a
per-run agent, but MemoryAgent below gives it the same BaseAgent
interface for consistency with everything else in the registry.

Each of these does real work rather than being a rename of existing
logic:
- MetadataAgent extracts structured bibliographic fields that were
  previously just implicit in free-text titles/abstracts.
- EmbeddingAgent computes and persists TF-IDF vectors via VectorStore
  (see memory/vector_store.py for the "why TF-IDF, not FAISS" note).
- ValidationAgent runs a checklist against a completed analysis before
  it becomes a PDF report, catching the empty-section problem that's
  invisible until someone's examiner opens the PDF.
- PlannerAgent is a lookup table from task name -> required agent
  names. Most of its value already existed structurally (each
  /api/agents/* endpoint only ever calls the one agent it needs) — this
  makes that decision explicit and inspectable rather than implicit in
  which route handler you happened to call.
"""
import re
from collections import Counter
from .base import BaseAgent


# ─── Metadata Agent ──────────────────────────────────────────────────
class MetadataAgent(BaseAgent):
    name = "metadata_agent"
    description = "Extracts structured bibliographic metadata (DOI, authors, venue, year) from fetched papers."

    def execute(self, context):
        papers = context.get('papers', [])
        records = []
        missing_doi = 0
        missing_authors = 0
        for p in papers:
            doi = p.get('doi') or ''
            authors = p.get('authors') or []
            if not doi:
                missing_doi += 1
            if not authors:
                missing_authors += 1
            records.append({
                'title': p.get('title', ''),
                'authors': authors,
                'year': p.get('year', 'N/A'),
                'venue': p.get('venue') or (p.get('categories') or ['N/A'])[0],
                'doi': doi,
                'source': p.get('source', ''),
            })
        context['metadata'] = records
        return {
            'metadata': records,
            'count': len(records),
            'missing_doi': missing_doi,
            'missing_authors': missing_authors,
        }

    def explain(self, context, data):
        return (f"Extracted structured metadata for {data['count']} papers "
                f"({data['missing_doi']} missing a DOI, {data['missing_authors']} missing author data — "
                f"expected for sources that don't expose them, e.g. some arXiv preprints).")


# ─── Embedding Agent ─────────────────────────────────────────────────
class EmbeddingAgent(BaseAgent):
    name = "embedding_agent"
    description = "Computes and persists TF-IDF embeddings for papers/document chunks into the vector store."

    def __init__(self, vector_store):
        self.vector_store = vector_store

    def execute(self, context):
        collection = context.get('embedding_collection')
        items = context.get('embedding_items', [])
        if not collection or not items:
            return {'stored': 0, 'collection': collection}
        stored = self.vector_store.add_batch(collection, items)
        return {'stored': stored, 'collection': collection}

    def explain(self, context, data):
        if not data.get('stored'):
            return "No items were provided to embed."
        return (f"Computed TF-IDF vectors for {data['stored']} items and stored them "
                f"under collection '{data['collection']}' for later similarity search.")


# ─── Validation Agent ────────────────────────────────────────────────
class ValidationAgent(BaseAgent):
    name = "validation_agent"
    description = "Checks a completed analysis for missing fields, empty sections, and duplicate papers before report generation."

    _REQUIRED_GAP_FIELDS = ['gap_title', 'description']

    def execute(self, context):
        analysis = context.get('analysis') or {}
        papers = context.get('papers', [])
        issues = []

        gaps = analysis.get('research_gaps', [])
        if not gaps:
            issues.append({'severity': 'high', 'section': 'research_gaps',
                            'message': 'No research gaps were identified — report will have an empty findings section.'})
        for i, gap in enumerate(gaps):
            for field in self._REQUIRED_GAP_FIELDS:
                if not gap.get(field) and not gap.get('title') and not gap.get('gap'):
                    issues.append({'severity': 'medium', 'section': f'research_gaps[{i}]',
                                    'message': f"Gap {i} is missing '{field}'."})
                    break

        if not analysis.get('emerging_trends'):
            issues.append({'severity': 'low', 'section': 'emerging_trends',
                            'message': 'No emerging trends were identified.'})

        titles = [re.sub(r'[^a-z0-9]+', ' ', (p.get('title') or '').lower()).strip() for p in papers]
        dupes = {t for t, c in Counter(titles).items() if t and c > 1}
        if dupes:
            issues.append({'severity': 'medium', 'section': 'papers',
                            'message': f'{len(dupes)} duplicate paper title(s) survived deduplication.'})

        if not papers:
            issues.append({'severity': 'high', 'section': 'papers',
                            'message': 'No related papers were fetched — trend/graph sections will be empty.'})

        high = sum(1 for i in issues if i['severity'] == 'high')
        return {'issues': issues, 'issue_count': len(issues), 'high_severity_count': high,
                'passed': high == 0}

    def explain(self, context, data):
        if not data['issue_count']:
            return "Validation passed — no missing fields, empty sections, or duplicate papers found."
        return (f"Found {data['issue_count']} issue(s) before report generation "
                f"({data['high_severity_count']} high-severity).")


# ─── Planner Agent ───────────────────────────────────────────────────
# Static task -> required agent-name mapping. This makes explicit what
# was already true implicitly (each /api/agents/* route only invokes
# the one agent it needs) — a genuinely dynamic planner (an LLM call
# deciding the plan) would add latency and cost to decide something
# this app already knows the answer to at the route level, so this
# stays a lookup table rather than a model call.
_TASK_PLANS = {
    'full_analysis': ['pdf_agent', 'search_agent', 'keyword_agent', 'metadata_agent',
                       'gap_detection_agent', 'knowledge_graph_agent', 'trend_agent',
                       'validation_agent', 'report_agent'],
    'ask_question': ['rag_assistant_agent'],
    'check_novelty': ['novelty_agent'],
    'generate_proposal': ['proposal_agent'],
    'design_experiment': ['experiment_agent'],
    'estimate_impact': ['impact_agent'],
    'explain_finding': ['explainability_agent'],
    'monitor_topic': ['monitoring_agent'],
}


class PlannerAgent(BaseAgent):
    name = "planner_agent"
    description = "Looks up which agents a requested task actually needs, so unrelated agents aren't run."

    def execute(self, context):
        task = context.get('task', '')
        plan = _TASK_PLANS.get(task)
        if not plan:
            return {'task': task, 'plan': [], 'known_tasks': sorted(_TASK_PLANS)}
        return {'task': task, 'plan': plan}

    def explain(self, context, data):
        if not data['plan']:
            return f"Unknown task '{data['task']}' — no plan available."
        return f"Task '{data['task']}' requires {len(data['plan'])} agent(s): {', '.join(data['plan'])}."


# ─── Memory Agent ────────────────────────────────────────────────────
class MemoryAgent(BaseAgent):
    name = "memory_agent"
    description = "Reads/writes conversation, research, gap, and proposal memory so follow-up requests can build on prior context without recomputing it."

    def __init__(self, context_memory):
        self.memory = context_memory

    def execute(self, context):
        op = context.get('memory_op', 'get')
        owner_uid = context['owner_uid']
        kind = context['kind']
        key = context.get('key', 'default')
        analysis_id = context.get('analysis_id')

        if op == 'set':
            self.memory.set(owner_uid, kind, key, context.get('value'), analysis_id)
            return {'op': 'set', 'kind': kind, 'key': key}
        elif op == 'append_conversation':
            history = self.memory.append_conversation(
                owner_uid, key, context.get('role', 'user'), context.get('content', ''), analysis_id)
            return {'op': 'append_conversation', 'history': history}
        elif op == 'list_keys':
            return {'op': 'list_keys', 'keys': self.memory.list_keys(owner_uid, kind, analysis_id)}
        else:
            value = self.memory.get(owner_uid, kind, key, analysis_id)
            return {'op': 'get', 'kind': kind, 'key': key, 'value': value}

    def explain(self, context, data):
        return f"Memory operation '{data['op']}' on kind '{context.get('kind')}'."
