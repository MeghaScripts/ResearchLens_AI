"""
Agents that wrap the app's existing, battle-tested functions (search,
PDF parsing, knowledge graph, Groq gap analysis, BibTeX, PDF report,
topic monitoring) as agent classes.

These deliberately do NOT reimplement any logic — each agent takes the
real function(s) as constructor arguments (dependency injection) so
app.py stays the single source of truth for how search/parsing/analysis
actually work, and the agent layer stays a thin orchestration wrapper
around it. This also avoids a circular import between app.py and the
agents package.
"""
from collections import Counter
from .base import BaseAgent


class SearchAgent(BaseAgent):
    name = "search_agent"
    description = "Searches arXiv, Semantic Scholar, and OpenAlex, merging and deduplicating results."

    def __init__(self, fetch_fn):
        self.fetch_fn = fetch_fn

    def execute(self, context):
        topic = context['topic']
        sources = context.get('paper_sources')  # optional list like ['ArXiv','OpenAlex']
        venue_filter = context.get('venue_filter')  # optional list like ['NeurIPS','ICML']
        kwargs = {'max_results': context.get('max_results', 500)}
        if sources:
            kwargs['sources'] = sources
        if venue_filter:
            kwargs['venue_filter'] = venue_filter
        papers = self.fetch_fn(topic, **kwargs)
        context['papers'] = papers
        return {'papers': papers, 'count': len(papers)}

    def explain(self, context, data):
        sources = sorted(set(p.get('source', '') for p in data['papers']))
        return (f"Queried {', '.join(sources) or 'no'} sources for "
                f"\"{context.get('topic', '')}\" and retrieved {data['count']} "
                f"deduplicated papers.")


class PDFAnalysisAgent(BaseAgent):
    name = "pdf_agent"
    description = "Extracts page-tagged and plain text from a single uploaded PDF."

    def __init__(self, extract_fn):
        self.extract_fn = extract_fn

    def execute(self, context):
        filepath = context.get('pdf_filepath')
        tagged, plain = ("", "")
        if filepath:
            tagged, plain = self.extract_fn(filepath)
        context['pdf_text'] = tagged
        context['pdf_plain'] = plain
        return {'pdf_text': tagged, 'pdf_plain': plain}

    def explain(self, context, data):
        pages = data['pdf_text'].count('[PAGE') if data['pdf_text'] else 0
        return (f"Extracted text from the uploaded PDF ({pages} tagged pages)."
                if pages else "No PDF was uploaded for this analysis.")


class MultiPDFAnalysisAgent(BaseAgent):
    name = "multi_pdf_agent"
    description = "Extracts text from multiple uploaded PDFs for cross-paper analysis."

    def __init__(self, extract_fn, upload_folder):
        self.extract_fn = extract_fn
        self.upload_folder = upload_folder

    def execute(self, context):
        import os, uuid
        pdf_files = context.get('pdf_files', [])
        papers_data = []
        for pdf_file in pdf_files:
            if pdf_file and pdf_file.filename:
                fname = f"{uuid.uuid4()}_{pdf_file.filename}"
                fpath = os.path.join(self.upload_folder, fname)
                pdf_file.save(fpath)
                tagged, plain = self.extract_fn(fpath)
                papers_data.append({'name': pdf_file.filename, 'text': tagged, 'plain': plain})
        context['papers_data'] = papers_data
        return {'papers_data': papers_data, 'count': len(papers_data)}

    def explain(self, context, data):
        return f"Extracted text from {data['count']} uploaded PDFs."


class KeywordExtractionAgent(BaseAgent):
    name = "keyword_agent"
    description = "Derives frequent single- and multi-word key concepts from document text and fetched abstracts."

    def __init__(self, keyword_fn):
        self.keyword_fn = keyword_fn

    def execute(self, context):
        papers_data = context.get('papers_data')
        if papers_data:
            doc_text = " ".join(pd.get('plain', '') for pd in papers_data)
        else:
            doc_text = context.get('pdf_plain', '')
        papers = context.get('papers', [])
        combined = doc_text + " " + " ".join(p.get('title', '') + ' ' + p.get('abstract', '') for p in papers)
        keywords = self.keyword_fn(combined, top_n=30)
        context['keywords'] = keywords
        return {'keywords': keywords}

    def explain(self, context, data):
        return (f"Derived {len(data['keywords'])} key concepts via frequency analysis "
                f"over the document text and {len(context.get('papers', []))} paper abstracts.")


class KnowledgeGraphAgent(BaseAgent):
    name = "knowledge_graph_agent"
    description = "Builds an entity graph (papers, authors, keywords, gaps, trends) with PageRank/community analytics."

    def __init__(self, build_fn, to_json_fn, discover_gaps_fn):
        self.build_fn = build_fn
        self.to_json_fn = to_json_fn
        self.discover_gaps_fn = discover_gaps_fn

    def execute(self, context):
        keywords = context.get('keywords', [])
        papers = context.get('papers', [])
        analysis = context.get('analysis')
        G = self.build_fn(keywords, papers, analysis)
        graph_data = self.to_json_fn(G)
        graph_data['suggested_gaps'] = self.discover_gaps_fn(G)
        context['graph'] = graph_data
        return {'graph': graph_data, 'node_count': G.number_of_nodes(), 'edge_count': G.number_of_edges()}

    def explain(self, context, data):
        n_suggested = len(data['graph'].get('suggested_gaps', []))
        return (f"Built a knowledge graph with {data['node_count']} nodes and "
                f"{data['edge_count']} edges (PageRank + community detection), then "
                f"flagged {n_suggested} structurally unexplored keyword-pair combinations.")


class GapDetectionAgent(BaseAgent):
    name = "gap_detection_agent"
    description = "Uses an LLM to identify concrete, evidenced research gaps from the topic, paper(s), and related literature."

    def __init__(self, single_fn, multi_fn):
        self.single_fn = single_fn
        self.multi_fn = multi_fn

    def execute(self, context):
        api_key = context['api_key']
        topic = context.get('topic', '')
        mode = context.get('mode', 'single')
        # Prefer the user's own explicit domain entry (form field
        # 'primary_domain'); fall back to DomainClassificationAgent's
        # real classification if the user didn't specify one. Either
        # way this is a real, sourced domain label — never guessed here
        # — passed through so the novelty score can be scoped to it
        # (see compute_grounded_novelty).
        primary_domain = context.get('primary_domain')
        if not primary_domain:
            classification = context.get('domain_classification') or {}
            primary_domain = (classification.get('primary_domain') or {}).get('name')
        if mode == 'multi':
            analysis = self.multi_fn(
                api_key, topic, context.get('papers_data', []),
                context.get('papers', []), context.get('keywords', []),
                primary_domain=primary_domain)
        else:
            analysis = self.single_fn(
                api_key, topic, context.get('pdf_text', ''),
                context.get('papers', []), context.get('keywords', []),
                primary_domain=primary_domain)
        context['analysis'] = analysis
        return {'analysis': analysis}

    def explain(self, context, data):
        n = len(data['analysis'].get('research_gaps', []))
        novelty = data['analysis'].get('novelty_score')
        scoped = data['analysis'].get('novelty_domain_scoped')
        novelty_note = (f" Novelty score {novelty} computed via real TF-IDF similarity"
                         f"{' (domain-scoped)' if scoped else ' (full corpus)'}."
                         if novelty is not None else "")
        return (f"Prompted the LLM with the topic, uploaded paper text, and "
                f"{len(context.get('papers', []))} related papers; it identified "
                f"{n} distinct research gaps with supporting evidence.{novelty_note}")


class TrendAnalysisAgent(BaseAgent):
    name = "trend_agent"
    description = "Derives publication-year and category trend distributions, plus LLM-identified emerging trends."

    def execute(self, context):
        papers = context.get('papers', [])
        year_counts = Counter(p.get('year', 'N/A') for p in papers)
        year_data = [{'year': y, 'count': c} for y, c in sorted(year_counts.items())]
        all_cats = []
        for p in papers:
            all_cats.extend(p.get('categories', []))
        cat_data = [{'category': k, 'count': v} for k, v in Counter(all_cats).most_common(8)]
        emerging = (context.get('analysis') or {}).get('emerging_trends', [])
        return {'year_data': year_data, 'cat_data': cat_data, 'emerging_trends': emerging}

    def explain(self, context, data):
        return (f"Aggregated publication-year and category distributions across "
                f"{len(context.get('papers', []))} papers and surfaced "
                f"{len(data['emerging_trends'])} LLM-identified emerging trends.")


class CitationAgent(BaseAgent):
    name = "citation_agent"
    description = "Formats fetched/uploaded papers into citation formats (BibTeX)."

    def __init__(self, bibtex_fn):
        self.bibtex_fn = bibtex_fn

    def execute(self, context):
        papers = context.get('papers', [])
        topic = context.get('topic', '')
        bibtex = self.bibtex_fn(papers, topic)
        return {'bibtex': bibtex, 'count': len(papers)}

    def explain(self, context, data):
        return f"Generated {data['count']} BibTeX entries, deduplicating citation keys."


class ReportGenerationAgent(BaseAgent):
    name = "report_agent"
    description = "Compiles the analysis, papers, and graph into a downloadable PDF report."

    def __init__(self, single_report_fn, multi_report_fn):
        self.single_report_fn = single_report_fn
        self.multi_report_fn = multi_report_fn

    def execute(self, context):
        mode = context.get('mode', 'single')
        topic = context.get('topic', '')
        analysis = context.get('analysis', {})
        report_path = context['report_path']
        if mode == 'multi':
            self.multi_report_fn(topic, analysis, context.get('papers_data', []), report_path)
        else:
            self.single_report_fn(topic, analysis, context.get('papers', []), report_path)
        return {'report_path': report_path}

    def explain(self, context, data):
        return "Rendered the full analysis, evidence, and trends into a formatted PDF report."


class MonitoringAgent(BaseAgent):
    name = "monitoring_agent"
    description = "Re-runs the search + gap-detection pipeline for a watched topic and diffs against the last snapshot."

    def __init__(self, run_check_fn):
        self.run_check_fn = run_check_fn

    def execute(self, context):
        result = self.run_check_fn(context['topic'], context['api_key'], context['watched_id'])
        return {'result': result}

    def explain(self, context, data):
        return "Re-ran the search and gap-detection pipeline for the watched topic and diffed it against the previous snapshot."