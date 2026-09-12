"""
Optional live web-search comparison (flowchart stage 15: compare the
collected literature against "current research" happening outside it —
new tools/models/datasets/industry requirements that may not be
reflected in the academic-paper corpus yet, since papers typically lag
real-world development by months to years).

There is no free, non-fabricated way to know "what's currently
happening in the field" without an actual live web search. So this
module is strictly opt-in: it only produces results when a real
search-provider API key is configured in this deployment, and returns
an empty list otherwise — never an invented summary of "current
trends" standing in for a search that didn't happen.
"""
import os
import requests

TIMEOUT = 20
DEFAULT_MAX_RESULTS = 6


def current_trends_available():
    """True if at least one real search-provider key is configured in
    this deployment, for callers that want to know upfront whether this
    feature will do anything before spending an LLM call formatting
    around it."""
    provider = (os.environ.get('CURRENT_TRENDS_SEARCH_PROVIDER') or '').strip().lower()
    if provider == 'tavily':
        return bool(os.environ.get('TAVILY_API_KEY'))
    if provider == 'serper':
        return bool(os.environ.get('SERPER_API_KEY'))
    if provider == 'both':
        return bool(os.environ.get('TAVILY_API_KEY')) or bool(os.environ.get('SERPER_API_KEY'))
    return False


def web_search_current_trends(topic, max_results=DEFAULT_MAX_RESULTS):
    """
    Runs a small number of real, live web searches for `topic` via
    whichever provider(s) are configured:
      CURRENT_TRENDS_SEARCH_PROVIDER = 'tavily'  + TAVILY_API_KEY
      CURRENT_TRENDS_SEARCH_PROVIDER = 'serper'  + SERPER_API_KEY
      CURRENT_TRENDS_SEARCH_PROVIDER = 'both'    + TAVILY_API_KEY and/or SERPER_API_KEY
    'both' calls each configured provider independently (so if only one
    of the two keys is actually set, it still runs on that one alone —
    same as if 'both' weren't set at all), merges their real results,
    and deduplicates by URL — a source appearing in both providers'
    results is kept once. `max_results` is split roughly evenly between
    the two calls so the combined total stays close to `max_results`
    rather than doubling it.
    Returns a list of real {'title', 'url', 'snippet'} results reflecting
    whatever the provider(s) actually returned — never a fabricated
    list — or [] if no provider is configured or every configured
    provider's request fails (network/quota errors are caught and
    logged per-provider, not raised, so one provider being down never
    takes down the other or the rest of the pipeline).
    """
    provider = (os.environ.get('CURRENT_TRENDS_SEARCH_PROVIDER') or '').strip().lower()
    if provider == 'tavily':
        return _search_tavily(topic, max_results)
    if provider == 'serper':
        return _search_serper(topic, max_results)
    if provider == 'both':
        half = max(1, max_results // 2)
        combined = []
        if os.environ.get('TAVILY_API_KEY'):
            combined.extend(_search_tavily(topic, half))
        if os.environ.get('SERPER_API_KEY'):
            combined.extend(_search_serper(topic, max_results - half))
        seen_urls = set()
        deduped = []
        for r in combined:
            url = r.get('url')
            if url and url in seen_urls:
                continue
            if url:
                seen_urls.add(url)
            deduped.append(r)
        return deduped[:max_results]
    return []  # no provider configured — nothing invented in its place


def _search_tavily(topic, max_results):
    api_key = os.environ.get('TAVILY_API_KEY')
    if not api_key:
        return []
    try:
        resp = requests.post(
            "https://api.tavily.com/search",
            json={
                'api_key': api_key,
                'query': f'{topic} latest research trends developments',
                'max_results': max_results,
                'search_depth': 'basic',
            },
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        results = resp.json().get('results', [])
        return [
            {'title': r.get('title', ''), 'url': r.get('url', ''), 'snippet': (r.get('content') or '')[:500]}
            for r in results
        ]
    except Exception as e:
        print(f"current_trends: Tavily search failed: {e}")
        return []


def _search_serper(topic, max_results):
    api_key = os.environ.get('SERPER_API_KEY')
    if not api_key:
        return []
    try:
        resp = requests.post(
            "https://google.serper.dev/search",
            headers={'X-API-KEY': api_key, 'Content-Type': 'application/json'},
            json={'q': f'{topic} latest research trends developments', 'num': max_results},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        organic = resp.json().get('organic', [])
        return [
            {'title': r.get('title', ''), 'url': r.get('link', ''), 'snippet': r.get('snippet', '')}
            for r in organic[:max_results]
        ]
    except Exception as e:
        print(f"current_trends: Serper search failed: {e}")
        return []