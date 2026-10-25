"""Level 3 (quality) validation against HUMAN ratings.

Quality is partly subjective, so there is no ground truth to generate: this module gives you the tools to
collect it and to measure the score against it.

    python -m eval.run_levels --make-quality-template 100      # writes eval/quality_ratings.csv
    # three people each fill rater1/rater2/rater3 with 1-5 (1 = poor, 5 = excellent) - see docs
    python -m eval.run_levels --ratings eval/quality_ratings.csv

Rating guide (give raters only the title / authors / venue / year / citations columns):
  5 excellent: clearly strong venue and impact for its age, credible authors, sound-looking contribution
  4 good       3 acceptable       2 weak       1 poor: dubious venue or no visible impact or credibility signals

Reported:
  * inter-rater agreement (mean pairwise quadratic-weighted kappa) - if raters disagree the target is noise
  * Spearman correlation between the shipped quality score and the mean rating
  * AUC for "high" (mean >= 4) versus "low" (mean <= 2) papers
  * the same after RIDGE-fitting component weights on training folds, scored on held-out folds
  * recommended DEEP_QUALITY_WEIGHTS (non-negative, rescaled to the shipped total)
This is a correlation with raters, never an "accuracy": claim it as exactly that.
"""
import csv
import itertools
import json
import os
import random

import paper_verification as pv
from eval import fit
from eval.run_eval import verify_once, stratified_folds

TEMPLATE_FIELDS = ['id', 'title', 'authors', 'year', 'venue', 'citation_count', 'doi', 'rater1', 'rater2', 'rater3']


def make_template(records, n, out_csv, seed=13):
    """Sample n real papers, spread evenly over topics, and write the blank rating sheet."""
    rng = random.Random(seed)
    real = [r for r in records if r.get('category') == 'real']
    by_topic = {}
    for r in real:
        by_topic.setdefault(r.get('topic') or '', []).append(r)
    for g in by_topic.values():
        rng.shuffle(g)
    picked, i = [], 0
    topics = sorted(by_topic)
    while len(picked) < min(n, len(real)):
        g = by_topic[topics[i % len(topics)]]
        if g:
            picked.append(g.pop())
        i += 1
        if i > 10 * (n + len(real)):
            break
    rng.shuffle(picked)
    os.makedirs(os.path.dirname(os.path.abspath(out_csv)), exist_ok=True)
    with open(out_csv, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=TEMPLATE_FIELDS)
        w.writeheader()
        for r in picked:
            w.writerow({'id': r['id'], 'title': r.get('title'), 'authors': '; '.join(r.get('authors') or []),
                        'year': r.get('year'), 'venue': r.get('venue'), 'citation_count': r.get('citation_count'),
                        'doi': r.get('doi'), 'rater1': '', 'rater2': '', 'rater3': ''})
    return len(picked)


def load_ratings(path):
    """-> list of {'id', 'ratings': [ints...], 'mean'} for rows with at least one valid 1-5 rating."""
    out = []
    with open(path, newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            vals = []
            for k in ('rater1', 'rater2', 'rater3'):
                try:
                    v = int(float(row.get(k) or ''))
                except ValueError:
                    continue
                if 1 <= v <= 5:
                    vals.append(v)
            if vals:
                out.append({'id': row['id'], 'ratings': vals, 'mean': sum(vals) / len(vals)})
    return out


def agreement(rows):
    """Mean pairwise weighted kappa over raters, using only rows rated by both raters of the pair."""
    full = [r for r in rows if len(r['ratings']) == 3]
    if len(full) < 10:
        return None
    ks = [fit.weighted_kappa([r['ratings'][a] for r in full], [r['ratings'][b] for r in full])
          for a, b in itertools.combinations(range(3), 2)]
    return sum(ks) / len(ks)


def _features(papers, comps, fill):
    X = []
    for p in papers:
        qc = p.get('quality_components') or {}
        X.append([qc[c]['value'] if c in qc else fill[c] for c in comps])
    return X


def evaluate(rows, records, k=5, seed=13):
    by_id = {r['id']: r for r in records}
    rows = [r for r in rows if r['id'] in by_id]
    if len(rows) < 20:
        return {'error': f'only {len(rows)} rated papers matched labelled.jsonl; rate at least 50 (100 is better)'}
    pairs = verify_once([by_id[r['id']] for r in rows])
    papers = [p for _rec, p in pairs]
    y = [r['mean'] for r in rows]
    shipped = [p.get('quality_score') or 0.0 for p in papers]

    report = {'n_rated': len(rows), 'kappa_mean_pairwise': agreement(rows),
              'spearman_shipped': fit.spearman(shipped, y)}
    hi = [(s, 1) for s, v in zip(shipped, y) if v >= 4]
    lo = [(s, 0) for s, v in zip(shipped, y) if v <= 2]
    if hi and lo:
        report['auc_high_vs_low_shipped'] = fit.auc([s for s, _ in hi + lo], [l for _, l in hi + lo])
        report['n_high'], report['n_low'] = len(hi), len(lo)

    comps = sorted(pv.QUALITY_WEIGHTS)
    present = {c: [p['quality_components'][c]['value'] for p in papers if c in (p.get('quality_components') or {})]
               for c in comps}
    fill = {c: (sum(v) / len(v) if v else 0.5) for c, v in present.items()}
    usable = [c for c in comps if len(present[c]) >= len(papers) / 2]
    X = _features(papers, usable, fill)

    items = list(range(len(rows)))
    folds = stratified_folds(items, key=lambda i: round(y[i]), k=min(k, max(2, len(rows) // 10)), seed=seed)
    oof = [0.0] * len(rows)
    for held in folds:
        held_set = set(held)
        tr = [i for i in items if i not in held_set]
        if not tr or not held:
            continue
        w, b = fit.ridge_fit([X[i] for i in tr], [y[i] for i in tr], lam=1.0)
        for i in held:
            oof[i] = b + sum(wj * xj for wj, xj in zip(w, X[i]))
    report['spearman_fitted_out_of_fold'] = fit.spearman(oof, y)
    if hi and lo:
        idx = [i for i, v in enumerate(y) if v >= 4 or v <= 2]
        report['auc_high_vs_low_fitted_out_of_fold'] = fit.auc([oof[i] for i in idx], [1 if y[i] >= 4 else 0 for i in idx])

    w, _b = fit.ridge_fit(X, y, lam=1.0)
    w = [max(0.0, v) for v in w]
    total_default = sum(pv.QUALITY_WEIGHTS[c] for c in usable)
    weights = dict(pv.QUALITY_WEIGHTS)
    if sum(w) > 0:
        for c, wc in zip(usable, w):
            weights[c] = round(total_default * wc / sum(w), 4)
    report['recommended_weights'] = weights
    report['components_fitted'] = usable
    return report


def format_report(rep):
    if 'error' in rep:
        return '== LEVEL 3 QUALITY ==\n  ' + rep['error']
    kap = rep['kappa_mean_pairwise']
    lines = [f"== LEVEL 3 QUALITY vs human ratings (n={rep['n_rated']}) ==",
             '  inter-rater kappa: ' + (f'{kap:.2f}' if kap is not None else 'n/a (need >= 10 papers rated by all 3)')
             + ('  <- low agreement: the target is noisy' if kap is not None and kap < 0.4 else ''),
             f"  Spearman, shipped score vs mean rating:      {rep['spearman_shipped']:.2f}",
             f"  Spearman, fitted weights (held-out folds):   {rep['spearman_fitted_out_of_fold']:.2f}"]
    if 'auc_high_vs_low_shipped' in rep:
        lines.append(f"  AUC high(>=4, n={rep['n_high']}) vs low(<=2, n={rep['n_low']}): shipped {rep['auc_high_vs_low_shipped']:.2f}"
                     f"  fitted {rep.get('auc_high_vs_low_fitted_out_of_fold', float('nan')):.2f}")
    lines.append("  recommended: DEEP_QUALITY_WEIGHTS='" + json.dumps(rep['recommended_weights']) + "'")
    return '\n'.join(lines)
