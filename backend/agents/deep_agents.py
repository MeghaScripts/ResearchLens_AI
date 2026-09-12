"""
Agents for the "deep research" pipeline: exhaustively page every checked
source, rank by real-signal impact score, shortlist to 100 per source,
combine, rank again, shortlist to 500, then detect research gaps from
how keyword usage across those 500 papers is shifting over time.

Same dependency-injection pattern as core_agents.py — each agent takes
the real functions as constructor arguments so deep_collection.py / app.py
stay the single source of truth for how collection and LLM calls actually
work, and this stays a thin orchestration wrapper.
"""
from .base import BaseAgent


class DeepCollectionAgent(BaseAgent):
    name = "deep_collection_agent"
    description = ("Flowchart stage 2 — selects sources relevant to the domain (general-"
                    "purpose sources always included; subject-specific databases like "
                    "PubMed/ERIC/INSPIRE-HEP/NASA ADS only when the domain signal actually "
                    "matches their documented scope) — then pages every selected source to "
                    "(near) exhaustion, ranks by impact score (including real author/"
                    "institution influence via Semantic Scholar + OpenAlex, where a real "
                    "author ID is available), shortlists the top 100 per source, and "
                    "combines/re-ranks to a top-500 overall shortlist.")

    def __init__(self, run_fn, registry_builder_fn):
        self.run_fn = run_fn                    # deep_collection.run_two_stage_collection
        self.registry_builder_fn = registry_builder_fn  # (context) -> {source_name: callable}

    def execute(self, context):
        topic = context['topic']
        registry = self.registry_builder_fn(context)
        context['deep_sources_selected'] = list(registry.keys())
        result = self.run_fn(topic, registry, extra_queries=context.get('deep_extra_queries'))
        context['deep_stage0_stats'] = result['stage0_stats']
        context['deep_stage1_stats'] = result['stage1_stats']
        context['deep_papers'] = result['stage2_papers']
        context['deep_citations_backfilled'] = result['citations_backfilled']
        context['deep_sources_with_citation_data'] = result['sources_with_citation_data']
        context['deep_sources_recency_only'] = result['sources_recency_only']
        context['deep_queries_used'] = result.get('queries_used', [topic])
        context['deep_authors_with_influence_data'] = result.get('authors_with_influence_data', 0)
        context['deep_institutions_with_influence_data'] = result.get('institutions_with_influence_data', 0)
        return result

    def explain(self, context, data):
        collected = sum(data.get('stage0_stats', {}).values())
        n_sources = len(data.get('sources_queried', []))
        n_final = data.get('stage2_count', 0)
        cited = len(data.get('sources_with_citation_data', []))
        backfilled = data.get('citations_backfilled', 0)
        n_authors = data.get('authors_with_influence_data', 0)
        n_institutions = data.get('institutions_with_influence_data', 0)
        skipped = context.get('deep_sources_skipped_by_domain') or []
        domain_note = (
            f" Skipped {', '.join(skipped)} as not domain-relevant to this topic "
            f"(a subject-specific database whose documented scope didn't match)."
            if skipped else
            " No subject-specific source was skipped — either the domain matched "
            "one of them, or the domain signal was ambiguous enough that all were kept."
        )
        return (f"Collected {collected} deduplicated papers across {n_sources} sources, "
                f"backfilled real citation counts for {backfilled} of them via Semantic "
                f"Scholar, shortlisted the top 100 per source by impact score, combined and "
                f"re-ranked to a final top {n_final}. {cited}/{n_sources} sources ended up "
                f"with real citation data (native or backfilled) to rank by; any papers left "
                f"over fell back to a recency-only proxy score, which is flagged per-paper. "
                f"Found real author-influence data (Semantic Scholar/OpenAlex h-index) for "
                f"{n_authors}/{n_final} papers and real institution-influence data (OpenAlex) "
                f"for {n_institutions}/{n_final} — absent, not guessed, for papers whose "
                f"source exposed no real author ID."
                f"{domain_note}")


class DomainUnderstandingAgent(BaseAgent):
    name = "domain_understanding_agent"
    description = ("Flowchart stage 1 — expands the raw topic/domain/keywords into real "
                    "related search-query strings via the LLM, so Stage-0 collection isn't "
                    "limited to one literal query.")

    def __init__(self, expand_fn):
        self.expand_fn = expand_fn  # app.py's analyze_domain_understanding_with_groq

    def execute(self, context):
        result = self.expand_fn(
            context['api_key'], context['topic'],
            domain=context.get('domain'), sub_domain=context.get('sub_domain'),
            keywords=context.get('keywords'), research_question=context.get('research_question'),
        )
        context['deep_domain_understanding'] = result
        context['deep_extra_queries'] = (result.get('expanded_queries') or [])[:5]
        return result

    def explain(self, context, data):
        n = len(data.get('expanded_queries', []))
        return (f"Identified domain '{data.get('primary_domain', '')}' and generated {n} "
                f"related search queries to widen Stage-0 collection beyond the literal "
                f"topic string alone.")


class CurrentTrendsAgent(BaseAgent):
    name = "current_trends_agent"
    description = ("Flowchart stage 15 — optionally runs a real live web search for what's "
                    "currently happening in the field outside the academic literature. "
                    "Strictly opt-in: produces nothing when no search-provider API key is "
                    "configured, rather than inventing a 'current trends' summary.")

    def __init__(self, search_fn, available_fn):
        self.search_fn = search_fn          # current_trends.web_search_current_trends
        self.available_fn = available_fn    # current_trends.current_trends_available

    def execute(self, context):
        if not self.available_fn():
            context['deep_current_trends'] = []
            context['deep_current_trends_available'] = False
            return {'results': [], 'available': False}
        results = self.search_fn(context['topic'])
        context['deep_current_trends'] = results
        context['deep_current_trends_available'] = bool(results)
        return {'results': results, 'available': bool(results)}

    def explain(self, context, data):
        if not data.get('available'):
            return ("No live-search provider is configured for this deployment, so this "
                    "step contributes nothing — the trend/gap analysis below is grounded "
                    "only in the collected literature, not in external 'current' claims.")
        return f"Pulled {len(data['results'])} real, live web results on current developments in this topic."


class FullTextAnalysisAgent(BaseAgent):
    name = "full_text_analysis_agent"
    description = ("Flowchart stages 4 & 12 — extracts methodology/dataset/limitations/"
                    "future-work for papers. Uses REAL full text (arXiv PDF, Europe PMC OA, "
                    "CORE) wherever it genuinely exists, and falls back to explicit, "
                    "labeled abstract-inference for every other paper, so 'analyze every "
                    "selected paper' isn't silently skipped for papers with no open-access "
                    "full text on file. Defaults to every paper passed in; `full_text_top_n` "
                    "can bound it to a cheaper/faster slice by impact rank.")

    def __init__(self, deep_analyze_fn, groq_model):
        self.deep_analyze_fn = deep_analyze_fn  # full_text_analysis.deep_analyze_top_papers
        self.groq_model = groq_model

    def execute(self, context):
        papers = context.get('deep_papers', [])
        top_n = context.get('full_text_top_n', None)  # None = every paper (stage 12)
        full_text_count, abstract_count = self.deep_analyze_fn(
            context['api_key'], papers, self.groq_model, top_n=top_n)
        context['deep_full_text_analyzed_count'] = full_text_count
        context['deep_abstract_inference_count'] = abstract_count
        attempted = len(papers) if top_n is None else min(top_n, len(papers))
        return {'full_text_count': full_text_count, 'abstract_count': abstract_count, 'attempted': attempted}

    def explain(self, context, data):
        return (f"Analyzed {data['attempted']} papers: {data['full_text_count']} from real "
                f"full text (arXiv/Europe PMC/CORE), {data['abstract_count']} via labeled "
                f"abstract-inference for papers with no open-access full text on file.")


class TrendForecastingAgent(BaseAgent):
    name = "trend_forecasting_agent"
    description = ("Flowchart stage 16 — a standalone forecast (past -> present -> future) "
                    "from the real keyword-trend signal (and live current-trends data, when "
                    "available), produced BEFORE and separately from gap detection so "
                    "forecasting isn't folded into the same LLM call as naming gaps.")

    def __init__(self, trend_stats_fn, forecast_fn):
        self.trend_stats_fn = trend_stats_fn  # deep_collection.keyword_trend_stats
        self.forecast_fn = forecast_fn        # app.py's forecast_research_trends_with_groq

    def execute(self, context):
        papers = context.get('deep_papers', [])
        api_key = context['api_key']
        topic = context.get('topic', '')
        # Computed once here and reused by TrendGapDetectionAgent below,
        # rather than recomputed — same real trend_stats feed both stages.
        trend_stats = self.trend_stats_fn(papers)
        current_trends = context.get('deep_current_trends') or []
        forecast = self.forecast_fn(api_key, topic, trend_stats, current_trends)
        context['deep_trend_stats'] = trend_stats
        context['deep_trend_forecast'] = forecast
        return {'trend_stats': trend_stats, 'forecast': forecast}

    def explain(self, context, data):
        n_directions = len(data['forecast'].get('future_research_directions', []))
        n_emerging = len(data['forecast'].get('emerging_research_areas', []))
        return (f"Forecast {n_emerging} emerging research area(s) and {n_directions} concrete "
                f"near-term direction(s), grounded in real keyword-trend signal "
                f"(and live current-trends data, if enabled) — computed before gap "
                f"detection so the gaps below can build on it.")


class TrendGapDetectionAgent(BaseAgent):
    name = "trend_gap_detection_agent"
    description = ("Compares keyword usage in recent vs. established papers from the "
                    "deep-collection shortlist, asks the LLM to identify research gaps "
                    "grounded in that trend shift (and the stage-16 forecast, if it ran), "
                    "then validates and prioritizes each gap against real evidence counts "
                    "from the collected corpus.")

    def __init__(self, trend_stats_fn, gap_analysis_fn, validate_fn):
        self.trend_stats_fn = trend_stats_fn      # deep_collection.keyword_trend_stats
        self.gap_analysis_fn = gap_analysis_fn     # app.py's analyze_deep_research_gaps_with_groq
        self.validate_fn = validate_fn             # deep_collection.validate_and_prioritize_gaps

    def execute(self, context):
        papers = context.get('deep_papers', [])
        api_key = context['api_key']
        topic = context.get('topic', '')
        # Reuse trend_stats from TrendForecastingAgent if it already ran
        # (same real signal for both stages); otherwise compute it here
        # so this agent still works standalone (e.g. forecasting skipped).
        trend_stats = context.get('deep_trend_stats') or self.trend_stats_fn(papers)
        current_trends = context.get('deep_current_trends') or []
        forecast = context.get('deep_trend_forecast')
        analysis = self.gap_analysis_fn(api_key, topic, papers, trend_stats, current_trends, forecast)
        analysis['research_gaps'] = self.validate_fn(analysis.get('research_gaps', []), papers, trend_stats)
        context['deep_trend_stats'] = trend_stats
        context['deep_analysis'] = analysis
        return {'trend_stats': trend_stats, 'analysis': analysis}

    def explain(self, context, data):
        n_gaps = len(data['analysis'].get('research_gaps', []))
        n_rising = len(data['trend_stats'].get('rising_keywords', []))
        n_active = sum(1 for g in data['analysis'].get('research_gaps', [])
                       if g.get('validation', {}).get('is_recently_active'))
        n_addressed = sum(1 for g in data['analysis'].get('research_gaps', [])
                          if g.get('validation', {}).get('appears_already_addressed'))
        return (f"Found {n_rising} keyword(s) rising in usage among the most recent "
                f"papers versus established literature, and used that plus the topic "
                f"to identify {n_gaps} trend-grounded research gap(s) with a near-term "
                f"outlook for each. {n_active}/{n_gaps} gaps have real corpus evidence "
                f"published in the last 2 years; {n_addressed}/{n_gaps} look already "
                f"addressed by dense, recent, cross-cited evidence and are penalized in "
                f"ranking accordingly. Gaps are ranked by a priority_score that blends "
                f"the LLM's confidence with that real evidence, not LLM confidence alone.")