"""
Claim-level verification of AI-generated research gaps.

Why this exists
---------------
`paper_quality.ground_gaps` and `deep_collection.validate_and_prioritize_gaps`
check that a cited paper TITLE exists in the collected corpus. That shows the
paper is real; it does not show the paper SUPPORTS the claim. This module adds
the missing layers, each of them independent of the others:

 1. Cite by ID, not by title (`paper_id_lines`, `resolve_evidence_ids`).
    The prompt numbers the corpus papers [P1]..[Pn] and asks the model for
    `evidence_ids`. An ID that is not in the corpus resolves to nothing, so a
    hallucinated paper cannot appear as evidence at all. Titles are only a
    fallback when the model returned no usable ID.

 2. Check the claim, not only the paper (`check_claims`). For every gap the
    cited papers' ABSTRACTS are shown to an LLM judge that labels the claim
    supported / partial / unsupported / contradicted (or `unverifiable` when
    no abstract exists - that is "could not check", never "failed").

 3. Check whether the gap is already addressed (`annotate_semantic_overlap`).
    The gap text is compared with the recent literature in the corpus by
    embedding similarity (TF-IDF fallback, labelled as such). The gap's own
    evidence papers are excluded, since they motivate the gap.

 4. Agreement between runs (`apply_consensus`). Gaps that recur across
    independently generated runs are kept / ranked higher; a gap that appeared
    in one run only is flagged. Off by default because it multiplies Groq calls.

Everything here is pure Python except the single injected `llm_json_fn`
(prompt -> dict) used by `check_claims`; tests inject a fake.

HONESTY NOTES
-------------
 * The judge is an LLM, so its label is evidence, not proof. The label is shown
   next to the gap and feeds the priority score; it never silently deletes a gap
   (set CLAIM_DROP_CONTRADICTED=1 to drop contradicted ones).
 * The semantic thresholds below are defaults chosen conservatively, not tuned
   on labelled data. Calibrate them with eval/ before quoting accuracy.
"""
import os
import re
import time

import paper_quality
from agents.textsim import tfidf_vectors, cosine

CLAIM_CHECK_ENABLED = os.environ.get('CLAIM_CHECK', '1') != '0'
CLAIM_CHECK_MAX = int(os.environ.get('CLAIM_CHECK_MAX', '50'))            # gaps judged per analysis
CLAIM_CHECK_GROUP = int(os.environ.get('CLAIM_CHECK_GROUP', '4'))         # gaps judged per LLM call
CLAIM_CHECK_PAPERS = int(os.environ.get('CLAIM_CHECK_PAPERS', '3'))       # abstracts shown per gap
CLAIM_CHECK_ABSTRACT_CHARS = int(os.environ.get('CLAIM_CHECK_ABSTRACT_CHARS', '700'))
CLAIM_CHECK_PAUSE_S = float(os.environ.get('CLAIM_CHECK_PAUSE_S', '2.5'))  # stay under 30 RPM
CLAIM_DROP_CONTRADICTED = os.environ.get('CLAIM_DROP_CONTRADICTED', '0') == '1'

# Semantic "already addressed" check.
ADDRESSED_RECENT_YEARS = int(os.environ.get('ADDRESSED_RECENT_YEARS', '3'))
ADDRESSED_MIN_HITS = int(os.environ.get('ADDRESSED_MIN_HITS', '2'))
ADDRESSED_THRESHOLD_EMBED = float(os.environ.get('ADDRESSED_THRESHOLD_EMBED', '0.72'))
ADDRESSED_THRESHOLD_TFIDF = float(os.environ.get('ADDRESSED_THRESHOLD_TFIDF', '0.45'))

# Consensus across runs.
CONSENSUS_SIMILARITY = float(os.environ.get('CONSENSUS_SIMILARITY', '0.35'))

LABELS = ('supported', 'partial', 'unsupported', 'contradicted', 'unverifiable')

# Added to the 0-1 priority score in deep_collection.validate_and_prioritize_gaps.
PRIORITY_ADJUSTMENT = {
    'supported': 0.05,
    'partial': 0.0,
    'unverifiable': 0.0,
    'unsupported': -0.15,
    'contradicted': -0.30,
}
# Sort rank for the standard path (lower = earlier). Stable, so severity order survives inside a group.
_SORT_RANK = {'supported': 0, 'partial': 1, 'unverifiable': 1, 'unsupported': 2, 'contradicted': 3}


# ───────────────────────── 1. cite by ID ─────────────────────────
def build_id_map(papers, start=1):
    """-> {'P1': paper, 'P2': paper, ...} in list order."""
    return {f'P{i}': p for i, p in enumerate(papers or [], start=start)}


def id_of(id_map):
    """-> {id(paper): 'P7'} so a line formatter can look an ID up from a paper object."""
    return {id(p): pid for pid, p in id_map.items()}


def paper_id_lines(papers, id_map, fmt=None):
    """One prompt line per paper, prefixed with its ID: '[P3] Title (2023)'."""
    ids = id_of(id_map)
    out = []
    for p in papers:
        pid = ids.get(id(p), '?')
        body = fmt(p) if fmt else f"{p.get('title', '')} ({p.get('year', 'N/A')})"
        body = body[2:] if body.startswith('- ') else body
        out.append(f'[{pid}] {body}')
    return '\n'.join(out)


_ID_RE = re.compile(r'^\s*\[?\s*p?\s*(\d+)\s*\]?\s*$', re.I)


def _norm_id(raw):
    if isinstance(raw, int):
        return f'P{raw}'
    if not isinstance(raw, str):
        return None
    m = _ID_RE.match(raw)
    return f'P{int(m.group(1))}' if m else None


def resolve_evidence_ids(gap, id_map):
    """Turn the model's `evidence_ids` into real papers.

    Sets on `gap`:
      evidence_ids          canonical IDs that exist in the corpus (deduplicated, order kept)
      evidence_ids_unknown  IDs the model returned that are NOT in the corpus (hallucinated)
      evidence_papers       titles of the resolved papers - replaces whatever titles the model typed
    If the model gave no usable ID the title list is left alone (title fallback in ground_gaps).
    Returns the gap."""
    raw = gap.get('evidence_ids')
    if isinstance(raw, (str, int)):
        raw = [raw]
    known, unknown = [], []
    for item in raw or []:
        pid = _norm_id(item)
        if pid is None:
            unknown.append(str(item))
        elif pid in id_map:
            if pid not in known:
                known.append(pid)
        else:
            unknown.append(pid)
    gap['evidence_ids'] = known
    gap['evidence_ids_unknown'] = unknown
    if known:
        gap['evidence_papers_llm_titles'] = list(gap.get('evidence_papers') or [])
        gap['evidence_papers'] = [id_map[pid].get('title') for pid in known]
    return gap


def resolve_all(gaps, id_map):
    for g in gaps or []:
        if isinstance(g, dict):
            resolve_evidence_ids(g, id_map)
    return gaps


# ───────────────────────── helpers ─────────────────────────
def _evidence_papers(gap, papers, index):
    """Real papers cited by this gap (IDs already turned into titles; title fuzzy-match otherwise)."""
    found, seen = [], set()
    cited = gap.get('evidence_papers') or []
    if isinstance(cited, str):
        cited = [cited]
    for title in cited:
        if not isinstance(title, str):
            continue
        p = paper_quality._match_paper(title, index)
        if p is not None and id(p) not in seen:
            seen.add(id(p))
            found.append(p)
    return found


def _title_index(papers):
    return [(p, paper_quality._norm_title(p.get('title')), paper_quality._title_tokens(p.get('title')))
            for p in papers or [] if p.get('title')]


def _abstract(p):
    ab = p.get('abstract')
    return ab.strip() if isinstance(ab, str) else ''


def _claim_text(gap):
    return ((gap.get('gap_title') or '').strip() + '. ' + (gap.get('description') or '').strip()).strip()


# ───────────────────────── 2. claim support ─────────────────────────
def _build_prompt(batch):
    parts = []
    for gid, gap, papers in batch:
        abstracts = '\n'.join(
            f"  ({n}) {p.get('title', '')}: {_abstract(p)[:CLAIM_CHECK_ABSTRACT_CHARS]}"
            for n, p in enumerate(papers, start=1))
        parts.append(f'CLAIM {gid}: {_claim_text(gap)}\nCITED ABSTRACTS:\n{abstracts}')
    body = '\n\n'.join(parts)
    return f"""You are a strict fact-checker for academic research-gap claims. For each CLAIM below, read ONLY the cited abstracts and decide whether they support the claim that this research gap exists.

Labels:
- supported: the abstracts directly show the limitation or missing coverage the claim asserts.
- partial: the abstracts are related and consistent with the claim but do not establish it.
- unsupported: the abstracts say nothing relevant to the claim.
- contradicted: the abstracts show the opposite, e.g. they already address the gap or the claim is false.

Use only what the abstracts say. Do not use outside knowledge. When unsure between two labels choose the weaker one.

{body}

Return pure JSON (no markdown):
{{"results": [{{"id": "g1", "label": "supported|partial|unsupported|contradicted", "reason": "one sentence, 25 words at most"}}]}}"""


def _normalise_label(label):
    label = str(label or '').strip().lower()
    return label if label in LABELS else None


def check_claims(gaps, papers, llm_json_fn, max_gaps=None, group=None, pause_s=None, log=print):
    """Label each gap's claim against the abstracts of the papers it cites.

    llm_json_fn(prompt) -> dict, e.g. a closure over groq_json.chat_json. It may raise; a failed call
    leaves its gaps as `unverifiable` (could not check), never as `unsupported`.

    Writes gap['claim_check'] = {label, reason, papers_checked, judge}. Returns a summary dict.
    Only the first `max_gaps` gaps (default CLAIM_CHECK_MAX) are judged; later ones get no block."""
    max_gaps = CLAIM_CHECK_MAX if max_gaps is None else max_gaps
    group = max(1, CLAIM_CHECK_GROUP if group is None else group)
    pause_s = CLAIM_CHECK_PAUSE_S if pause_s is None else pause_s
    index = _title_index(papers)
    counts = {label: 0 for label in LABELS}
    calls = 0
    todo = []

    for n, gap in enumerate(gaps or []):
        if not isinstance(gap, dict) or n >= max_gaps:
            continue
        cited = _evidence_papers(gap, papers, index)[:CLAIM_CHECK_PAPERS * 2]
        with_abstract = [p for p in cited if len(_abstract(p)) >= 40][:CLAIM_CHECK_PAPERS]
        if not with_abstract:
            gap['claim_check'] = {
                'label': 'unverifiable',
                'reason': 'no cited paper has an abstract to check against' if cited
                          else 'no cited paper could be resolved',
                'papers_checked': 0, 'judge': None}
            counts['unverifiable'] += 1
            continue
        todo.append((gap, with_abstract))

    for start in range(0, len(todo), group):
        chunk = todo[start:start + group]
        batch = [(f'g{i}', gap, ps) for i, (gap, ps) in enumerate(chunk, start=1)]
        results = {}
        try:
            if calls:
                time.sleep(pause_s)
            calls += 1
            data = llm_json_fn(_build_prompt(batch))
            for r in (data or {}).get('results', []) if isinstance(data, dict) else []:
                if isinstance(r, dict) and _normalise_label(r.get('label')):
                    results[str(r.get('id'))] = r
        except Exception as e:                       # includes quota errors: stop spending, keep what we have
            log(f'[claim-check] judge call failed ({type(e).__name__}: {e}) - those gaps stay unverifiable')
            if type(e).__name__ == 'GroqQuotaExhausted':
                for gid, gap, ps in batch:
                    gap['claim_check'] = {'label': 'unverifiable', 'reason': 'judge quota exhausted',
                                          'papers_checked': len(ps), 'judge': None}
                    counts['unverifiable'] += 1
                for later_gap, later_ps in todo[start + group:]:
                    later_gap['claim_check'] = {'label': 'unverifiable', 'reason': 'judge quota exhausted',
                                                'papers_checked': len(later_ps), 'judge': None}
                    counts['unverifiable'] += 1
                break
        for gid, gap, ps in batch:
            r = results.get(gid)
            if r:
                label = _normalise_label(r.get('label'))
                gap['claim_check'] = {'label': label, 'reason': str(r.get('reason') or '')[:300],
                                      'papers_checked': len(ps), 'judge': 'llm'}
            else:
                label = 'unverifiable'
                gap['claim_check'] = {'label': label, 'reason': 'judge returned no usable verdict',
                                      'papers_checked': len(ps), 'judge': None}
            counts[label] += 1

    return {'judged': sum(counts.values()), 'llm_calls': calls, 'counts': counts}


def order_by_support(gaps):
    """Stable sort: supported first, contradicted last; severity order is preserved within a group.
    Gaps with no claim_check block sort with 'partial'."""
    def key(g):
        return _SORT_RANK.get(((g.get('claim_check') or {}).get('label')), 1)
    return sorted(gaps, key=key)


def drop_contradicted(gaps):
    kept = [g for g in gaps if (g.get('claim_check') or {}).get('label') != 'contradicted']
    return kept, len(gaps) - len(kept)


# ───────────────────────── 3. already addressed? ─────────────────────────
def _year(p):
    try:
        return int(str(p.get('year') or '')[:4])
    except ValueError:
        return None


def _paper_text(p):
    return ((p.get('title') or '') + '. ' + _abstract(p)[:500]).strip()


def annotate_semantic_overlap(gaps, papers, recent_years=None, threshold=None, min_hits=None):
    """For each gap, count recent corpus papers (excluding the gap's own evidence) whose text is very
    similar to the gap statement. Two or more hits mean the gap may already be worked on.

    Sets gap['claim_check']['semantic_addressed'] (bool), ['addressed_by'] (up to 3 titles with year and
    similarity) and ['similarity_method']. Returns the method used ('sentence_transformer' or
    'tfidf_fallback') or None when there was nothing to compare."""
    recent_years = ADDRESSED_RECENT_YEARS if recent_years is None else recent_years
    min_hits = ADDRESSED_MIN_HITS if min_hits is None else min_hits
    years = [y for y in (_year(p) for p in papers or []) if y]
    if not gaps or not years:
        return None
    cutoff = max(years) - recent_years + 1
    pool = [p for p in papers if (_year(p) or 0) >= cutoff and p.get('title')]
    if not pool:
        return None

    gap_texts = [_claim_text(g) for g in gaps]
    pool_texts = [_paper_text(p) for p in pool]
    method, sims = 'tfidf_fallback', None
    try:
        import semantic_similarity
        vecs = semantic_similarity.embed_batch(gap_texts + pool_texts)
        if vecs is not None:
            n = len(gap_texts)
            sims = [[max(0.0, semantic_similarity._cosine_np(vecs[i], vecs[n + j])) for j in range(len(pool))]
                    for i in range(n)]
            method = 'sentence_transformer'
    except Exception:
        sims = None
    if sims is None:
        tv = tfidf_vectors(gap_texts + pool_texts)
        n = len(gap_texts)
        sims = [[cosine(tv[i], tv[n + j]) for j in range(len(pool))] for i in range(n)]

    thr = threshold if threshold is not None else (
        ADDRESSED_THRESHOLD_EMBED if method == 'sentence_transformer' else ADDRESSED_THRESHOLD_TFIDF)
    index = _title_index(papers)
    for gi, gap in enumerate(gaps):
        own = {id(p) for p in _evidence_papers(gap, papers, index)}
        hits = sorted(((sims[gi][j], pool[j]) for j in range(len(pool))
                       if id(pool[j]) not in own and sims[gi][j] >= thr), key=lambda t: -t[0])
        block = gap.setdefault('claim_check', {})
        block['semantic_addressed'] = len(hits) >= min_hits
        block['addressed_by'] = [{'title': p.get('title'), 'year': _year(p), 'similarity': round(s, 3)}
                                 for s, p in hits[:3]]
        block['similarity_method'] = method
        block['similarity_threshold'] = thr
    return method


# ───────────────────────── 4. agreement between runs ─────────────────────────
def _gap_vectors(gap_lists):
    texts = [_claim_text(g) for gl in gap_lists for g in gl]
    return tfidf_vectors(texts) if texts else []


def apply_consensus(primary, other_runs, min_agree=2, similarity=None, min_keep=10):
    """Count in how many runs each primary gap also appears (a near-identical gap, by TF-IDF cosine of
    title+description). Writes gap['agreement'] = {runs_agreeing, runs_total}.

    Keeps gaps with agreement >= min_agree, but never fewer than `min_keep` (falls back to ranking by
    agreement, so a strict filter cannot empty the report). -> (kept, dropped_count)."""
    similarity = CONSENSUS_SIMILARITY if similarity is None else similarity
    runs = [primary] + [r for r in other_runs if r]
    vecs = _gap_vectors(runs)
    offsets, pos = [], 0
    for r in runs:
        offsets.append(pos)
        pos += len(r)
    for i, gap in enumerate(primary):
        agree = 1
        for ri in range(1, len(runs)):
            if any(cosine(vecs[i], vecs[offsets[ri] + j]) >= similarity for j in range(len(runs[ri]))):
                agree += 1
        gap['agreement'] = {'runs_agreeing': agree, 'runs_total': len(runs)}
    ranked = sorted(primary, key=lambda g: -g['agreement']['runs_agreeing'])
    kept = [g for g in ranked if g['agreement']['runs_agreeing'] >= min_agree]
    if len(kept) < min(min_keep, len(primary)):
        kept = ranked[:max(min_keep, len(kept))]
    kept_ids = {id(g) for g in kept}
    ordered = [g for g in primary if id(g) in kept_ids]       # restore the original (severity) order
    return ordered, len(primary) - len(ordered)


# ───────────────────────── one-call pipeline ─────────────────────────
def annotate_gaps(gaps, papers, llm_json_fn=None, log=print):
    """Run the claim check (if an LLM function is given) and the semantic already-addressed check.
    Returns (gaps, summary). Gaps are reordered so contradicted/unsupported claims sink."""
    summary = {'enabled': CLAIM_CHECK_ENABLED, 'claim_check': None, 'similarity_method': None, 'dropped': 0}
    if not gaps:
        return gaps, summary
    if llm_json_fn is not None and CLAIM_CHECK_ENABLED:
        summary['claim_check'] = check_claims(gaps, papers, llm_json_fn, log=log)
    try:
        summary['similarity_method'] = annotate_semantic_overlap(gaps, papers)
    except Exception as e:                                   # an advisory signal must never fail the analysis
        log(f'[claim-check] semantic overlap skipped ({type(e).__name__}: {e})')
    if CLAIM_DROP_CONTRADICTED:
        gaps, summary['dropped'] = drop_contradicted(gaps)
    gaps = order_by_support(gaps)
    return gaps, summary
