"""
Per-paper analysis (flowchart stages 4 & 12: methodology / dataset /
algorithms / research objectives / limitations / future work).

Two tiers, both explicitly labeled so a caller/reader always knows
which one produced a given field:

  - Real full text (extraction_basis='full_text'): only three of this
    app's sources ever expose real, freely-accessible full text for at
    least some of their papers —
      - arXiv: every paper's PDF is freely downloadable.
      - Europe PMC: open-access articles (`is_open_access` True, with a
        `pmcid`) have a real full-text XML endpoint.
      - CORE: many indexed works carry a real `fullText` field for
        papers the aggregator has actually ingested full text for.
    The LLM here never sees a paper without also seeing that paper's
    real, extracted text, and is told to say "not stated" rather than
    guess when a field genuinely isn't in that text.

  - Abstract-only inference (extraction_basis='abstract_inference'):
    used for every other paper, so flowchart stage 12 ("analyze every
    selected paper") isn't silently skipped for the ~90%+ of papers
    with no full text on file anywhere. The prompt is explicit with
    the LLM that it only has a title+abstract, asks it to infer what
    it reasonably can from that alone, and to say "not inferable from
    the abstract" rather than invent detail an abstract wouldn't
    contain (e.g. exact hyperparameters). This tier is real LLM
    output, but it is lower-confidence than full-text extraction by
    construction — every result carries `extraction_basis` so nothing
    downstream can conflate the two.

deep_analyze_top_papers runs full-text analysis first (real text takes
priority whenever it's available) and then abstract-inference on the
remainder, over up to `top_n` papers (default: every paper passed in —
`None`/omitted means all, matching stage 12's "analyze every selected
paper"; pass a smaller top_n to trade coverage for a shorter/cheaper
run). Calls run concurrently (bounded worker pool) since this is
I/O-bound (network fetch + LLM call) and this module may now be asked
to process the full Stage-2 pool of up to 500 papers in one run.
"""
import os
import re
import io
import json
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from groq import Groq

TIMEOUT = 30
FULLTEXT_TOP_N_DEFAULT = None  # None = every paper passed in (flowchart stage 12)
MAX_TEXT_CHARS = 12000  # how much of a paper's full text is sent to the LLM per call
MAX_WORKERS = int(os.environ.get('DEEP_ANALYSIS_MAX_WORKERS', '8'))


def _fetch_arxiv_pdf_text(arxiv_id):
    import PyPDF2
    url = f"https://arxiv.org/pdf/{arxiv_id}"
    resp = requests.get(url, timeout=TIMEOUT, headers={'User-Agent': 'ResearchLensAI/1.0'})
    resp.raise_for_status()
    reader = PyPDF2.PdfReader(io.BytesIO(resp.content))
    text = "\n".join((page.extract_text() or '') for page in reader.pages)
    return text.strip()


def _fetch_europe_pmc_fulltext(pmcid):
    clean_id = pmcid if pmcid.upper().startswith('PMC') else f'PMC{pmcid}'
    url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{clean_id}/fullTextXML"
    resp = requests.get(url, timeout=TIMEOUT)
    resp.raise_for_status()
    # Strip XML tags to plain text — this is the article's real body
    # text, not a summary or abstract.
    text = re.sub(r'<[^>]+>', ' ', resp.text)
    return re.sub(r'\s+', ' ', text).strip()


def _fetch_core_fulltext(core_id, api_key):
    url = f"https://api.core.ac.uk/v3/works/{core_id}"
    resp = requests.get(url, timeout=TIMEOUT, headers={'Authorization': f'Bearer {api_key}'})
    resp.raise_for_status()
    data = resp.json()
    return (data.get('fullText') or '').strip()


def get_real_full_text(paper):
    """
    Returns (text, source_label) if real full text could be fetched for
    this paper from a source that actually provides it, else
    (None, None). Never falls back to the abstract or fabricates
    anything — a paper this can't get real text for simply isn't
    deep-analyzed.
    """
    try:
        if paper.get('source') == 'arXiv' and paper.get('arxiv_id'):
            text = _fetch_arxiv_pdf_text(paper['arxiv_id'])
            if text:
                return text, 'arxiv_pdf'
        if paper.get('source') == 'Europe PMC' and paper.get('is_open_access') and paper.get('pmcid'):
            text = _fetch_europe_pmc_fulltext(paper['pmcid'])
            if text:
                return text, 'europe_pmc_fulltext'
        if paper.get('source') == 'CORE' and paper.get('core_id'):
            api_key = os.environ.get('CORE_API_KEY')
            if api_key:
                text = _fetch_core_fulltext(paper['core_id'], api_key)
                if text:
                    return text, 'core_fulltext'
    except Exception as e:
        print(f"full_text_analysis: full-text fetch failed for '{paper.get('title')}': {e}")
    return None, None


def analyze_paper_full_text_with_groq(api_key, paper, full_text, groq_model, max_chars=MAX_TEXT_CHARS):
    """
    Asks the LLM to extract methodology/dataset/algorithms/objectives/
    limitations/future-work — grounded in this paper's REAL full text
    (truncated to `max_chars` to fit the prompt), never its abstract
    alone. Explicitly instructed to say a field isn't stated rather than
    infer one, so this stays extraction rather than guessing.
    """
    client = Groq(api_key=api_key)
    text_slice = full_text[:max_chars]
    prompt = f"""Extract the following from the ACTUAL TEXT of this paper below. If the text does not clearly state a field, respond "Not stated in the available text" for that field — do NOT infer or guess.

PAPER TITLE: {paper.get('title', '')}

PAPER TEXT (truncated to the first ~{max_chars} characters):
{text_slice}

Respond with pure JSON, no markdown:
{{
  "research_objective": "...",
  "methodology": "...",
  "dataset": "...",
  "algorithms_models": "...",
  "key_results": "...",
  "limitations": "...",
  "future_work": "..."
}}"""
    response = client.chat.completions.create(
        model=groq_model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2, max_tokens=1200,
    )
    raw = response.choices[0].message.content.strip()
    raw = re.sub(r'^```[a-z]*\n?', '', raw)
    raw = re.sub(r'\n?```$', '', raw)
    return json.loads(raw)


def analyze_paper_from_abstract_with_groq(api_key, paper, groq_model):
    """
    Fallback for papers with no real full text available anywhere:
    asks the LLM to infer the same fields from title+abstract alone,
    explicitly told it only has an abstract (not the full paper) and
    to say "not inferable from the abstract" for anything an abstract
    genuinely wouldn't state (e.g. specific dataset splits, exact
    hyperparameters) rather than invent it. This is real LLM output,
    just lower-confidence by construction than full-text extraction —
    every result is tagged extraction_basis='abstract_inference' so
    it's never confused with the full_text tier.
    """
    client = Groq(api_key=api_key)
    abstract = (paper.get('abstract') or '').strip()
    if not abstract:
        return None
    prompt = f"""You only have this paper's TITLE and ABSTRACT below — not its full text. Infer what you reasonably can about each field from that alone. If the abstract genuinely doesn't give enough to say, respond "Not inferable from the abstract" for that field — do NOT invent specifics (exact dataset splits, hyperparameters, numeric results) an abstract wouldn't contain.

PAPER TITLE: {paper.get('title', '')}

ABSTRACT:
{abstract}

Respond with pure JSON, no markdown:
{{
  "research_objective": "...",
  "methodology": "...",
  "dataset": "...",
  "algorithms_models": "...",
  "key_results": "...",
  "limitations": "...",
  "future_work": "..."
}}"""
    response = client.chat.completions.create(
        model=groq_model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2, max_tokens=800,
    )
    raw = response.choices[0].message.content.strip()
    raw = re.sub(r'^```[a-z]*\n?', '', raw)
    raw = re.sub(r'\n?```$', '', raw)
    return json.loads(raw)


def _analyze_one(api_key, paper, groq_model):
    """Full-text tier first; abstract-inference fallback; explicit
    'unavailable' only when neither could produce anything (no full
    text AND no abstract, or both calls errored)."""
    text, source_label = get_real_full_text(paper)
    if text:
        try:
            extracted = analyze_paper_full_text_with_groq(api_key, paper, text, groq_model)
            extracted['full_text_available'] = True
            extracted['full_text_source'] = source_label
            extracted['extraction_basis'] = 'full_text'
            return paper, extracted, True
        except Exception as e:
            print(f"full_text_analysis: full-text LLM extraction failed for '{paper.get('title')}': {e}")
            # fall through to abstract inference rather than giving up entirely

    try:
        extracted = analyze_paper_from_abstract_with_groq(api_key, paper, groq_model)
    except Exception as e:
        print(f"full_text_analysis: abstract-inference LLM extraction failed for '{paper.get('title')}': {e}")
        extracted = None

    if extracted is not None:
        extracted['full_text_available'] = False
        extracted['extraction_basis'] = 'abstract_inference'
        return paper, extracted, False

    return paper, {'full_text_available': False, 'extraction_basis': None}, False


def deep_analyze_top_papers(api_key, papers, groq_model, top_n=FULLTEXT_TOP_N_DEFAULT):
    """
    Runs per-paper analysis (full-text tier where available, else
    abstract-inference — see module docstring) over `top_n` papers by
    impact score. `top_n=None` (the default) analyzes every paper
    passed in, matching flowchart stage 12's "analyze every selected
    paper"; pass an integer to bound the run to a cheaper/faster slice.

    Mutates each analyzed paper with a `deep_analysis` field carrying
    `extraction_basis` ('full_text' | 'abstract_inference' | None) plus
    the extracted fields, or just `{'full_text_available': False,
    'extraction_basis': None}` when neither tier could produce
    anything (e.g. no abstract and no full text at all).

    Returns (full_text_analyzed_count, abstract_inference_count).
    """
    target = papers if top_n is None else papers[:top_n]
    full_text_count = 0
    abstract_count = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_analyze_one, api_key, paper, groq_model): paper for paper in target}
        for future in as_completed(futures):
            paper, extracted, was_full_text = future.result()
            paper['deep_analysis'] = extracted
            if was_full_text:
                full_text_count += 1
            elif extracted.get('extraction_basis') == 'abstract_inference':
                abstract_count += 1

    return full_text_count, abstract_count