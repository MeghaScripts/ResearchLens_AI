"""
Basic sanity filter for fetched paper metadata.

Open registries (Crossref especially) contain test/junk deposits such as
title "drgdsrg", authors "AI AI", no abstract, no venue, no citations. These
polluted the analysed paper set. is_credible_paper() rejects only records that
are clearly junk; legitimate papers with a missing abstract still pass as long
as they carry other real bibliographic signals.
"""
import re

_VOWELS = set('aeiouyAEIOUY')
_PLACEHOLDER_AUTHORS = {'ai ai', 'unknown', 'anonymous', 'n/a', 'na', 'none', 'test', 'test test', 'author', 'admin'}


def _words(title):
    return re.findall(r"[A-Za-z]{2,}", title or '')


def looks_like_gibberish(title):
    title = (title or '').strip()
    if len(title) < 8:
        return True
    words = _words(title)
    if len(words) < 2:
        # single "word" titles: only OK when it is long and word-like
        return not (len(title) >= 14 and any(c in _VOWELS for c in title))
    bad = 0
    for w in words:
        wl = w.lower()
        if len(wl) >= 4 and not any(c in _VOWELS for c in wl):
            bad += 1
        elif re.search(r'[^aeiouy]{6,}', wl):
            bad += 1
        elif len(wl) >= 5 and len(set(wl)) <= 2:
            bad += 1
    if bad / len(words) >= 0.4:
        return True
    # keyboard-mash single-token titles like "dfgdf asdasd"
    if len(words) <= 3 and all(len(w) <= 8 for w in words) and bad >= 1:
        return True
    return False


def _placeholder_authors(authors):
    names = [str(a).strip().lower() for a in (authors or []) if a and str(a).strip()]
    if not names:
        return True
    if all(n in _PLACEHOLDER_AUTHORS for n in names):
        return True
    for n in names:
        toks = n.split()
        if len(toks) >= 2 and len(set(toks)) == 1:      # "ai ai"
            continue
        return False
    return True


def is_credible_paper(p):
    title = (p.get('title') or '').strip()
    if looks_like_gibberish(title):
        return False
    abstract = (p.get('abstract') or '').strip()
    has_abstract = len(abstract) >= 40 and 'no abstract' not in abstract.lower()
    try:
        cites = int(p.get('citation_count') or 0)
    except (TypeError, ValueError):
        cites = 0
    has_venue = bool((p.get('venue') or '').strip())
    real_authors = not _placeholder_authors(p.get('authors'))
    signals = sum([has_abstract, has_venue, cites > 0, real_authors])
    # Curated indexes: gibberish check above + at least one real signal is enough.
    if (p.get('source') or '') in ('ArXiv', 'Semantic Scholar', 'OpenAlex', 'DBLP', 'PubMed', 'Europe PMC'):
        return signals >= 1
    # Open registries (Crossref etc.): need two independent real-metadata
    # signals, or a real abstract.
    if has_abstract:
        return signals >= 2 or len(abstract) >= 200
    return signals >= 2


def filter_credible(papers):
    return [p for p in papers if is_credible_paper(p)]