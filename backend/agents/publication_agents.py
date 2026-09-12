"""
Domain Classification Agent.

Classifies the user's research topic/idea into a primary domain +
secondary domains from the Publication Intelligence taxonomy
(backend/publications.py), using the existing dependency-free TF-IDF
module (textsim.py) — no network calls, no LLM required, so it can't
fail or slow down the existing pipeline for reasons unrelated to it.
"""
from .base import BaseAgent


class DomainClassificationAgent(BaseAgent):
    name = "domain_classification_agent"
    description = "Classifies the research topic into a domain/sub-domain taxonomy (society, publisher, journal, conference)."

    def __init__(self, classify_fn, get_db_fn):
        self.classify_fn = classify_fn
        self.get_db_fn = get_db_fn

    def execute(self, context: dict) -> dict:
        topic = context.get('topic', '')
        keywords = context.get('keywords', [])
        query_text = topic if not keywords else f"{topic} {' '.join(keywords[:12])}"
        conn = self.get_db_fn()
        try:
            result = self.classify_fn(conn, query_text)
        finally:
            conn.close()
        context['domain_classification'] = result
        return result

    def explain(self, context, data):
        primary = (data.get('primary_domain') or {}).get('name')
        if not primary:
            return "Could not confidently classify the topic into a known domain."
        n_secondary = len(data.get('secondary_domains') or [])
        return f"Classified topic as '{primary}'" + (f" (+{n_secondary} related domains)." if n_secondary else ".")
