"""
New enterprise-level agents: Research Assistant (RAG), Novelty
Verification, Research Proposal, Experiment Recommendation, Research
Impact, and Explainability.

Each talks to Groq directly (same model + JSON-cleaning convention the
rest of the app already uses) rather than reaching back into app.py, to
keep this package import-safe and independent of Flask/app state.
"""
import os
import json
import re
from collections import Counter

from groq import Groq

from .base import BaseAgent, trace_to_dicts
from .textsim import most_similar, chunk_text

GROQ_MODEL = os.environ.get('GROQ_MODEL', 'openai/gpt-oss-120b')


def _clean_json(raw):
    raw = raw.strip()
    raw = re.sub(r'^```[a-z]*\n?', '', raw)
    raw = re.sub(r'\n?```$', '', raw)
    # Strip bad escapes the LLM sometimes emits, same as the rest of the app
    raw = re.sub(r'\\(?!["\\bfnrtu/]|u[0-9a-fA-F]{4})', '', raw)
    return raw


class RAGResearchAssistantAgent(BaseAgent):
    name = "rag_assistant_agent"
    description = "Answers questions over uploaded paper(s) using retrieval-augmented generation."

    def execute(self, context):
        api_key = context['api_key']
        question = context['question']
        doc_text = context.get('document_text', '') or ''
        chunks = chunk_text(doc_text, 900, 150)
        if not chunks:
            return {'answer': "No document text is available to answer from — upload or re-analyze a paper first.",
                     'retrieved_chunks': 0}

        top = most_similar(question, chunks, top_n=5)
        context_blocks = [chunks[i] for i, score in top if score > 0]
        if not context_blocks:
            context_blocks = chunks[:3]
        context_text = "\n---\n".join(context_blocks)[:6000]

        client = Groq(api_key=api_key)
        prompt = f"""You are a research assistant answering questions strictly using the provided document excerpts. If the excerpts do not contain the answer, say so clearly instead of guessing.

DOCUMENT EXCERPTS:
{context_text}

QUESTION: {question}

Answer concisely (3-6 sentences) and note which excerpt(s) (by position, e.g. "excerpt 2") support each claim."""
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2, max_tokens=1000,
        )
        answer = response.choices[0].message.content.strip()
        return {'answer': answer, 'retrieved_chunks': len(context_blocks)}

    def explain(self, context, data):
        return (f"Retrieved the {data.get('retrieved_chunks', 0)} passages most similar to "
                f"the question via TF-IDF cosine similarity, and grounded the answer in them.")


class NoveltyVerificationAgent(BaseAgent):
    name = "novelty_agent"
    description = "Checks how similar a proposed research idea is to existing literature and produces a novelty score."

    def execute(self, context):
        idea = context['idea']
        papers = context.get('papers', [])
        corpus = [f"{p.get('title', '')}. {p.get('abstract', '')}" for p in papers]
        top = most_similar(idea, corpus, top_n=5)

        closest = []
        for idx, score in top:
            if score <= 0:
                continue
            p = papers[idx]
            closest.append({
                'title': p.get('title'), 'year': p.get('year'),
                'similarity_pct': round(score * 100, 1),
            })

        if not closest:
            novelty_score = None
        else:
            max_sim = max(c['similarity_pct'] for c in closest)
            novelty_score = round(max(0, 100 - max_sim), 1)

        reasoning = None
        api_key = context.get('api_key')
        if api_key and closest:
            client = Groq(api_key=api_key)
            titles = "\n".join(
                f"- {c['title']} ({c['similarity_pct']}% textual similarity)" for c in closest[:5])
            prompt = f"""A researcher proposes this idea:
"{idea}"

The most textually similar existing papers found (via TF-IDF) are:
{titles}

In 3-4 sentences, assess whether the proposed idea appears genuinely novel relative to these, or substantially overlaps with existing work. Be specific about what would need to differ for it to be novel."""
            try:
                response = client.chat.completions.create(
                    model=GROQ_MODEL, messages=[{"role": "user", "content": prompt}],
                    temperature=0.3, max_tokens=400,
                )
                reasoning = response.choices[0].message.content.strip()
            except Exception as e:
                reasoning = f"(LLM reasoning unavailable: {e})"

        return {
            'novelty_score': novelty_score,
            'closest_matches': closest,
            'reasoning': reasoning,
            'compared_against': len(papers),
        }

    def explain(self, context, data):
        n = data.get('compared_against', 0)
        score = data.get('novelty_score')
        return (f"Compared the idea against {n} candidate papers via TF-IDF cosine similarity; "
                f"novelty score ({score if score is not None else 'n/a'}) is 100 minus the "
                f"closest match's similarity.")


class ResearchProposalAgent(BaseAgent):
    name = "proposal_agent"
    description = "Generates a structured research proposal from a chosen gap or topic."

    def execute(self, context):
        api_key = context['api_key']
        topic = context.get('topic', '')
        gap = context.get('gap') or {}
        client = Groq(api_key=api_key)
        gap_text = json.dumps(gap) if gap else "No specific gap provided — propose based on the topic generally."
        prompt = f"""Generate a formal research proposal as pure JSON (no markdown, no commentary).

TOPIC: {topic}
TARGET RESEARCH GAP: {gap_text}

{{
  "title": "Proposed research title",
  "problem_statement": "2-4 sentences",
  "objectives": ["Objective 1", "Objective 2", "Objective 3"],
  "methodology": "Detailed paragraph describing approach, data, and evaluation plan",
  "expected_outcomes": ["Outcome 1", "Outcome 2"],
  "timeline": [{{"phase": "Phase name", "duration": "e.g. Months 1-3", "activities": "description"}}],
  "required_resources": ["resource 1", "resource 2"],
  "risks_and_mitigations": [{{"risk": "...", "mitigation": "..."}}]
}}"""
        response = client.chat.completions.create(
            model=GROQ_MODEL, messages=[{"role": "user", "content": prompt}],
            temperature=0.4, max_tokens=2000,
        )
        raw = _clean_json(response.choices[0].message.content)
        return json.loads(raw)

    def explain(self, context, data):
        return (f"Drafted a proposal titled \"{data.get('title', '')}\" with "
                f"{len(data.get('objectives', []))} objectives, grounded in the selected research gap.")


class ExperimentRecommendationAgent(BaseAgent):
    name = "experiment_agent"
    description = "Suggests datasets, evaluation metrics, and baseline models for a proposed study."

    def execute(self, context):
        api_key = context['api_key']
        topic = context.get('topic', '')
        gap = context.get('gap') or {}
        client = Groq(api_key=api_key)
        prompt = f"""Suggest a concrete experimental design as pure JSON (no markdown, no commentary).

TOPIC: {topic}
RESEARCH GAP / IDEA: {json.dumps(gap) if gap else topic}

{{
  "recommended_datasets": [{{"name": "...", "why": "...", "notes": "public availability / access notes"}}],
  "evaluation_metrics": [{{"metric": "...", "why": "..."}}],
  "baseline_models": [{{"model": "...", "why_baseline": "..."}}],
  "experimental_setup_notes": "short paragraph on splits, compute, and reproducibility considerations"
}}"""
        response = client.chat.completions.create(
            model=GROQ_MODEL, messages=[{"role": "user", "content": prompt}],
            temperature=0.3, max_tokens=1500,
        )
        raw = _clean_json(response.choices[0].message.content)
        return json.loads(raw)

    def explain(self, context, data):
        return (f"Recommended {len(data.get('recommended_datasets', []))} datasets, "
                f"{len(data.get('evaluation_metrics', []))} metrics, and "
                f"{len(data.get('baseline_models', []))} baselines for the study.")


class ResearchImpactAgent(BaseAgent):
    name = "impact_agent"
    description = "Estimates potential research impact from publication-trend signals plus LLM reasoning."

    def execute(self, context):
        papers = context.get('papers', [])
        topic = context.get('topic', '')

        year_counts = Counter(p.get('year', 'N/A') for p in papers if p.get('year') not in (None, 'N/A'))
        years_sorted = sorted((y for y in year_counts if str(y).isdigit()))
        recent_years = years_sorted[-3:] if len(years_sorted) >= 3 else years_sorted
        recent_activity = sum(year_counts[y] for y in recent_years)
        total = sum(year_counts.values()) or 1
        growth_ratio = round(recent_activity / total, 2)
        heuristic_score = min(100, round(growth_ratio * 70 + min(len(papers), 50) / 50 * 30))

        reasoning = None
        api_key = context.get('api_key')
        if api_key:
            client = Groq(api_key=api_key)
            year_summary = ", ".join(f"{y}: {c}" for y, c in sorted(year_counts.items())) or "no year data"
            prompt = f"""Given publication activity by year for the topic "{topic}": {year_summary}.
Recent-3-year share of activity: {growth_ratio * 100:.0f}%. Total papers considered: {len(papers)}.

In 3-4 sentences, assess the likely research impact/momentum of new work in this area right now, and what would maximize its impact (timing, venue, positioning)."""
            try:
                response = client.chat.completions.create(
                    model=GROQ_MODEL, messages=[{"role": "user", "content": prompt}],
                    temperature=0.3, max_tokens=400,
                )
                reasoning = response.choices[0].message.content.strip()
            except Exception as e:
                reasoning = f"(LLM reasoning unavailable: {e})"

        return {
            'impact_score': heuristic_score,
            'recent_activity_share': growth_ratio,
            'total_papers_considered': len(papers),
            'year_distribution': dict(year_counts),
            'reasoning': reasoning,
        }

    def explain(self, context, data):
        return (f"Derived a heuristic impact score ({data.get('impact_score')}/100) from "
                f"publication recency/volume across {data.get('total_papers_considered', 0)} "
                f"fetched papers, and added LLM reasoning on positioning.")


class ExplainabilityAgent(BaseAgent):
    name = "explainability_agent"
    description = "Explains, in plain language, why a research gap or recommendation was surfaced."

    def execute(self, context):
        target = context.get('target') or {}
        agent_explanations = trace_to_dicts(context.get('_trace', []))

        narrative = None
        api_key = context.get('api_key')
        if api_key:
            client = Groq(api_key=api_key)
            prompt = f"""Explain, for a non-technical researcher, why the following finding was surfaced by an AI research-gap analysis pipeline. Use plain language, 3-5 sentences, and reference the evidence.

FINDING: {json.dumps(target)[:2000]}

PIPELINE STEPS AND WHAT EACH CONTRIBUTED:
{json.dumps(agent_explanations, indent=2)[:3000]}

Write the explanation now."""
            try:
                response = client.chat.completions.create(
                    model=GROQ_MODEL, messages=[{"role": "user", "content": prompt}],
                    temperature=0.3, max_tokens=400,
                )
                narrative = response.choices[0].message.content.strip()
            except Exception as e:
                narrative = f"(LLM narrative unavailable: {e})"

        return {'narrative': narrative, 'pipeline_trace': agent_explanations}

    def explain(self, context, data):
        return f"Synthesized an explanation from {len(data.get('pipeline_trace', []))} upstream pipeline steps."
