"""Level 2 (relevance) evaluation, built from the SAME labelled papers - no new network calls.

Every real paper in labelled.jsonl belongs to one topic. Pairing each paper with each topic gives
  label 1  paper vs its own topic
  label 0  paper vs another topic
so 150 real papers x 6 topics = 900 labelled (paper, topic) pairs instead of a handful of off-topic
papers. For each topic the relevance scorer runs over the whole real corpus (as in the app, where the
IDF comes from the corpus being scored).

Reported (pooled out-of-fold, k folds split by paper so one paper never sits in both train and test):
  * the SHIPPED weights and cutoff (DEEP_RELEVANCE_MIN) - the honest baseline
  * weights fitted by non-negative logistic regression + a cutoff chosen on the training folds only
and AUC / balanced accuracy / precision / recall for each.

Limit: papers here carry title and abstract only, so the keyword/category components (which need
registry metadata) are absent; only components present on at least half the rows are fitted and the
others keep their shipped weights.
"""
import copy

import paper_verification as pv
from eval import fit
from eval.run_eval import _fresh_paper, stratified_folds


def real_papers(records):
    real = [r for r in records if r.get('category') == 'real' and r.get('topic')]
    topics = sorted({r['topic'] for r in real})
    return real, topics


def score_pairs(real, topics):
    """-> rows [{'id','topic','label','score','components','method'}], one per (paper, topic)."""
    base = [_fresh_paper(r) for r in real]
    rows = []
    for topic in topics:
        papers = copy.deepcopy(base)
        pv.compute_relevance_scores(papers, topic)
        for rec, p in zip(real, papers):
            comps = {c: v['value'] for c, v in (p.get('relevance_components') or {}).items()}
            rows.append({'id': rec['id'], 'topic': topic, 'label': 1 if rec['topic'] == topic else 0,
                         'score': p.get('relevance_score') or 0.0, 'components': comps,
                         'method': p.get('relevance_similarity_method')})
    return rows


def _contrib(value):
    return min(1.0, value / 0.5)          # the same saturation compute_relevance_scores applies


def rescore(row, weights):
    """The app's formula (renormalised over the components present) with other weights."""
    present = [c for c in row['components'] if weights.get(c, 0) > 0]
    avail = sum(weights[c] for c in present)
    if avail <= 0:
        return 0.0
    return 100.0 * sum(weights[c] * _contrib(row['components'][c]) for c in present) / avail


def fit_weights(rows, defaults=None):
    """Non-negative logistic regression over the components present on >= half the rows. Returns the full
    weight table (fitted components rescaled to keep their combined shipped weight; the rest unchanged)."""
    defaults = dict(defaults or pv.RELEVANCE_WEIGHTS)
    counts = {}
    for r in rows:
        for c in r['components']:
            counts[c] = counts.get(c, 0) + 1
    comps = sorted(c for c, n in counts.items() if n >= len(rows) / 2 and c in defaults)
    if not comps:
        return defaults
    X = [[_contrib(r['components'].get(c, 0.0)) for c in comps] for r in rows]
    y = [r['label'] for r in rows]
    # balance the 1:(T-1) imbalance so the fit does not simply learn "everything is off-topic"
    pos = sum(y) or 1
    neg = (len(y) - sum(y)) or 1
    Xw, yw = [], []
    for xi, yi in zip(X, y):
        reps = max(1, round(neg / pos)) if yi else 1
        for _ in range(reps):
            Xw.append(xi)
            yw.append(yi)
    w, _ = fit.logistic_fit(Xw, yw, nonneg=True)
    total_default = sum(defaults[c] for c in comps)
    wsum = sum(w)
    out = dict(defaults)
    if wsum > 0:
        for c, wc in zip(comps, w):
            out[c] = round(total_default * wc / wsum, 4)
    return out


def best_threshold(scores, labels):
    """Cutoff maximising balanced accuracy on the given rows."""
    best_t, best = 0.0, -1.0
    for t in sorted(set(round(s, 2) for s in scores)) + [100.0]:
        tp = sum(1 for s, l in zip(scores, labels) if l and s >= t)
        tn = sum(1 for s, l in zip(scores, labels) if not l and s < t)
        p, n = sum(labels), len(labels) - sum(labels)
        bal = 0.5 * (tp / p if p else 0) + 0.5 * (tn / n if n else 0)
        if bal > best:
            best_t, best = t, bal
    return best_t


def metrics_at(scores, labels, threshold):
    tp = sum(1 for s, l in zip(scores, labels) if l and s >= threshold)
    fp = sum(1 for s, l in zip(scores, labels) if not l and s >= threshold)
    tn = sum(1 for s, l in zip(scores, labels) if not l and s < threshold)
    fn = sum(1 for s, l in zip(scores, labels) if l and s < threshold)
    p, n = tp + fn, tn + fp
    recall = tp / p if p else 0.0
    spec = tn / n if n else 0.0
    return {'threshold': threshold, 'n': len(labels), 'positives': p,
            'auc': fit.auc(scores, labels), 'balanced_accuracy': 0.5 * (recall + spec),
            'accuracy': (tp + tn) / len(labels) if labels else 0.0,
            'precision': tp / (tp + fp) if (tp + fp) else 0.0, 'recall': recall,
            'specificity': spec, 'confusion': {'tp': tp, 'fp': fp, 'tn': tn, 'fn': fn}}


def evaluate(rows, real, k=5, seed=13, shipped_threshold=None):
    """-> report dict with 'shipped', 'fitted' (pooled out-of-fold) and 'recommended' (fit on everything)."""
    shipped_threshold = pv.RELEVANCE_DISCARD_THRESHOLD if shipped_threshold is None else shipped_threshold
    defaults = dict(pv.RELEVANCE_WEIGHTS)
    folds = stratified_folds(real, key=lambda r: r['topic'], k=k, seed=seed)
    by_id = {}
    for r in rows:
        by_id.setdefault(r['id'], []).append(r)

    shipped_scores = [r['score'] for r in rows]
    labels_all = [r['label'] for r in rows]
    report = {'pairs': len(rows), 'papers': len(real), 'k': k,
              'similarity_method': sorted({r['method'] for r in rows if r['method']}),
              'shipped': metrics_at(shipped_scores, labels_all, shipped_threshold)}

    pooled_scores, pooled_labels, pooled_thr = [], [], []
    for i, held in enumerate(folds):
        held_ids = {r['id'] for r in held}
        train_rows = [r for r in rows if r['id'] not in held_ids]
        held_rows = [r for r in rows if r['id'] in held_ids]
        if not train_rows or not held_rows:
            continue
        w = fit_weights(train_rows, defaults)
        thr = best_threshold([rescore(r, w) for r in train_rows], [r['label'] for r in train_rows])
        pooled_thr.append(thr)
        pooled_scores.extend(rescore(r, w) - thr + 50.0 for r in held_rows)     # centre on 50 so one cutoff pools
        pooled_labels.extend(r['label'] for r in held_rows)
    if pooled_scores:
        report['fitted_out_of_fold'] = metrics_at(pooled_scores, pooled_labels, 50.0)
        report['fitted_out_of_fold']['thresholds_per_fold'] = pooled_thr

    w_all = fit_weights(rows, defaults)
    thr_all = best_threshold([rescore(r, w_all) for r in rows], labels_all)
    report['recommended'] = {'weights': {c: v for c, v in w_all.items() if c != 'cross_encoder' or v},
                             'threshold': thr_all}
    return report


def format_report(rep):
    def line(name, m):
        return (f"  {name:26} AUC {m['auc']:.3f}  balanced acc {m['balanced_accuracy']:.1%}  "
                f"precision {m['precision']:.1%}  recall {m['recall']:.1%}  (cutoff {m['threshold']:.0f}, n={m['n']})")
    lines = [f"== LEVEL 2 RELEVANCE: {rep['pairs']} (paper, topic) pairs from {rep['papers']} papers; "
             f"similarity method: {', '.join(rep['similarity_method']) or 'n/a'} ==",
             line('shipped weights + cutoff', rep['shipped'])]
    if 'fitted_out_of_fold' in rep:
        lines.append(line(f"fitted ({rep['k']}-fold, held-out)", rep['fitted_out_of_fold']))
    rec = rep['recommended']
    lines.append(f"  recommended: DEEP_RELEVANCE_MIN={rec['threshold']:.0f}")
    lines.append("               DEEP_RELEVANCE_WEIGHTS='" + __import__('json').dumps(rec['weights']) + "'")
    return '\n'.join(lines)
