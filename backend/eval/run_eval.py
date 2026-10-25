"""Measure (and honestly tune) paper verification against the labelled set.

    python -m eval.build_eval_set                       # once, needs network
    python -m eval.run_eval                             # baseline on the held-out 30% test split
    python -m eval.run_eval --tune --target 0.95        # pick thresholds on the 70% train split only

(run from backend/). The first run calls the real registries and caches every answer in
eval/registry_cache.json; later runs and the threshold search replay the cache (use --cache-only to
forbid network access).

Protocol, so the number is defensible:
  * stratified 70/30 split with a fixed seed (every kind of fake appears in both halves);
  * the two decision thresholds (DEEP_AUTHENTICITY_MIN, DEEP_RELEVANCE_MIN) are searched on TRAIN only;
  * the headline numbers are measured on TEST only, for both the shipped defaults and the tuned values;
  * two figures are reported together: coverage (share of papers decided, the rest go to "review") and
    decisive accuracy (accuracy over the decided ones) with a Wilson 95% interval.
"""
import argparse
import copy
import json
import os
import random
import sys

import paper_verification as pv
from eval import metrics
from eval.build_eval_set import load_jsonl, split_train_test
from eval import cache as registry_cache

_PAPER_KEYS = ('title', 'authors', 'year', 'doi', 'venue', 'abstract', 'citation_count', 'source')
AUTH_GRID = (60, 65, 70, 75, 80, 85, 90)
REL_GRID = (20, 25, 30, 35, 40, 45, 50, 55)


def _fresh_paper(rec):
    p = {k: copy.deepcopy(rec.get(k)) for k in _PAPER_KEYS if rec.get(k) is not None}
    p['impact_score'] = float(rec.get('citation_count') or 0)
    p['_eval_id'] = rec['id']
    return p


def verify_once(records):
    """Run the full (network/cache-backed) verification ONCE per topic group. -> [(record, paper)].
    The expensive part; thresholds are applied afterwards by decide(), which is instant."""
    by_topic = {}
    for r in records:
        by_topic.setdefault(r.get('topic') or '', []).append(r)
    pairs = []
    for topic, group in by_topic.items():
        papers = [_fresh_paper(r) for r in group]
        pv.verify_papers(papers, topic, network_top_n=0)
        truth = {r['id']: r for r in group}
        pairs.extend((truth[p['_eval_id']], p) for p in papers)
    return pairs


def decide(pairs, auth_min=None, rel_min=None):
    """Re-apply the verdict rules to already-verified papers with the given thresholds (defaults: the
    shipped ones) -> [{'id','category','label','prediction','status'}]. decide_verdict only reads scores
    already stored on the paper, so this is exactly what a full re-run would give, without the cost."""
    old = (pv.AUTHENTICITY_REJECT_THRESHOLD, pv.RELEVANCE_DISCARD_THRESHOLD)
    if auth_min is not None:
        pv.AUTHENTICITY_REJECT_THRESHOLD = auth_min
    if rel_min is not None:
        pv.RELEVANCE_DISCARD_THRESHOLD = rel_min
    try:
        out = []
        for rec, paper in pairs:
            pv.decide_verdict(paper)
            out.append({'id': rec['id'], 'category': rec['category'], 'label': rec['label'],
                        'prediction': pv.decision_band(paper), 'status': paper.get('verification_status')})
        return out
    finally:
        pv.AUTHENTICITY_REJECT_THRESHOLD, pv.RELEVANCE_DISCARD_THRESHOLD = old


def evaluate(records, auth_min=None, rel_min=None):
    """Verify `records` and return per-record predictions at the given thresholds."""
    return decide(verify_once(records), auth_min, rel_min)


def _score(preds):
    return metrics.summarise((r['label'], r['prediction']) for r in preds)


def tune_pairs(pairs, target=0.95, auth_grid=AUTH_GRID, rel_grid=REL_GRID):
    """Grid-search the two thresholds on already-verified TRAIN pairs. Objective: highest coverage among
    settings whose decisive accuracy reaches `target`; if none reaches it, the highest decisive accuracy
    (coverage breaks ties). Each grid point only re-applies the verdict rules (instant).
    -> (best, table) with best = {'auth_min','rel_min','coverage','decisive_accuracy','met_target'}."""
    table = []
    for a in auth_grid:
        for r in rel_grid:
            s = _score(decide(pairs, a, r))
            table.append({'auth_min': a, 'rel_min': r, 'coverage': s['coverage'],
                          'decisive_accuracy': s['decisive_accuracy'], 'decided': s['decided']})
    ok = [t for t in table if t['decided'] and t['decisive_accuracy'] >= target]
    if ok:
        best = max(ok, key=lambda t: (t['coverage'], t['decisive_accuracy']))
        best = dict(best, met_target=True)
    else:
        best = max((t for t in table if t['decided']), default=table[0],
                   key=lambda t: (t['decisive_accuracy'], t['coverage']))
        best = dict(best, met_target=False)
    return best, table


def tune(train, target=0.95, auth_grid=AUTH_GRID, rel_grid=REL_GRID):
    """Verify `train` records once, then tune_pairs()."""
    return tune_pairs(verify_once(train), target, auth_grid, rel_grid)


def stratified_folds(items, key, k, seed=13):
    """k folds with every category spread across them. -> list of k lists."""
    rng = random.Random(seed)
    groups = {}
    for it in items:
        groups.setdefault(key(it), []).append(it)
    folds = [[] for _ in range(k)]
    n = 0
    for g in sorted(groups):
        members = groups[g][:]
        rng.shuffle(members)
        for m in members:
            folds[n % k].append(m)
            n += 1
    return folds


def cv_evaluate(pairs, k=5, target=0.95, auth_grid=AUTH_GRID, rel_grid=REL_GRID, seed=13, tune_thresholds=True):
    """k-fold cross-validation over verified (record, paper) pairs. For each fold the thresholds are tuned on
    the other k-1 folds only, then applied to the held-out fold; every record therefore gets exactly one
    out-of-fold prediction and the pooled metrics use ALL records (a tighter interval than one 30% split).
    -> (pooled_predictions, chosen) where chosen lists the thresholds picked per fold."""
    folds = stratified_folds(pairs, key=lambda p: p[0]['category'], k=k, seed=seed)
    preds, chosen = [], []
    for i, held in enumerate(folds):
        if not held:
            continue
        if tune_thresholds:
            train = [p for j, f in enumerate(folds) if j != i for p in f]
            best, _ = tune_pairs(train, target, auth_grid, rel_grid)
            chosen.append({k_: best[k_] for k_ in ('auth_min', 'rel_min', 'met_target')})
            preds.extend(decide(held, best['auth_min'], best['rel_min']))
        else:
            preds.extend(decide(held))
    return preds, chosen


def run_cv(args, handle):
    records = load_jsonl(args.data)
    pairs = verify_once(records)
    print(f'[eval] {len(records)} records verified once; {args.cv}-fold cross-validation, '
          f'thresholds tuned inside each training split only')
    preds, chosen = cv_evaluate(pairs, k=args.cv, target=args.target, seed=args.seed)
    s = _score(preds)
    print(metrics.format_report(f'POOLED OUT-OF-FOLD ({args.cv}-fold, n={len(preds)})', s))
    by_cat = metrics.by_category(preds)
    print('\nPer category (pooled): decided / n  and decisive accuracy')
    for cat, c in by_cat.items():
        print(f"  {cat:16} {c['decided']:>3}/{c['n']:<3}  {c['decisive_accuracy']:.0%}")
    print('thresholds chosen per fold:', chosen)
    report = {'mode': f'{args.cv}-fold CV', 'strict': bool(args.strict), 'n': len(preds), 'pooled': s,
              'pooled_by_category': by_cat, 'thresholds_per_fold': chosen}
    base = _score(decide(pairs))
    report['all_records_defaults'] = base
    print('\n' + metrics.format_report('ALL RECORDS at the shipped default thresholds (no tuning)', base))
    return report


def run(args):
    handle = registry_cache.install(args.cache, cache_only=args.cache_only, drop_prefixes=args.drop_cache or ())
    if args.strict:
        pv.STRICT_FIELD_REJECT = True
        print('[eval] STRICT mode: one severe title/author mismatch against the resolved registry record rejects')
    try:
        if args.cv:
            report = run_cv(args, handle)
            print(f"\n[cache] hits={handle.stats['hits']} misses={handle.stats['misses']} stored={handle.stats['stored']}")
            os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
            with open(args.out, 'w', encoding='utf-8') as f:
                json.dump(report, f, indent=2, default=str)
            print(f'[eval] wrote {args.out}')
            return report
        records = load_jsonl(args.data)
        train, test = split_train_test(records, test_fraction=0.30, seed=args.seed)
        print(f'[eval] {len(records)} records -> train {len(train)} / test {len(test)}')

        report = {'n_total': len(records), 'n_train': len(train), 'n_test': len(test), 'target': args.target,
                  'strict': bool(args.strict)}
        test_pairs = verify_once(test)
        base_preds = decide(test_pairs)
        base = _score(base_preds)
        report['baseline_test'] = base
        report['baseline_test_by_category'] = metrics.by_category(base_preds)
        print(metrics.format_report(
            f'BASELINE (defaults: authenticity>={pv.AUTHENTICITY_REJECT_THRESHOLD}, relevance>={pv.RELEVANCE_DISCARD_THRESHOLD}) on held-out TEST', base))

        if args.tune:
            best, table = tune(train, target=args.target)
            report['tuning'] = {'best_on_train': best, 'grid': table}
            print(f"\n[tune] best on TRAIN: authenticity>={best['auth_min']}, relevance>={best['rel_min']}, "
                  f"train coverage {best['coverage']:.1%}, train decisive accuracy {best['decisive_accuracy']:.1%}"
                  f"{'' if best['met_target'] else '  (target NOT reached)'}")
            tuned_preds = decide(test_pairs, best['auth_min'], best['rel_min'])
            tuned = _score(tuned_preds)
            report['tuned_test'] = tuned
            report['tuned_test_by_category'] = metrics.by_category(tuned_preds)
            print(metrics.format_report('TUNED thresholds on held-out TEST', tuned))
            print(f"\nSet on the server: DEEP_AUTHENTICITY_MIN={best['auth_min']} DEEP_RELEVANCE_MIN={best['rel_min']}")

        print('\nPer category (test, baseline): decided / n  and decisive accuracy')
        for cat, s in report['baseline_test_by_category'].items():
            print(f"  {cat:16} {s['decided']:>3}/{s['n']:<3}  {s['decisive_accuracy']:.0%}")
        print(f"\n[cache] hits={handle.stats['hits']} misses={handle.stats['misses']} stored={handle.stats['stored']}")
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, default=str)
        print(f'[eval] wrote {args.out}')
        return report
    finally:
        handle.save()
        handle.restore()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--data', default='eval/labelled.jsonl')
    ap.add_argument('--cache', default='eval/registry_cache.json')
    ap.add_argument('--out', default='eval/results.json')
    ap.add_argument('--tune', action='store_true', help='search thresholds on the train split')
    ap.add_argument('--target', type=float, default=0.95, help='decisive-accuracy target for --tune')
    ap.add_argument('--strict', action='store_true', help='reject on one severe title/author mismatch (opt-in rule)')
    ap.add_argument('--cv', type=int, default=0, metavar='K', help='k-fold cross-validation over ALL records (pooled out-of-fold metrics)')
    ap.add_argument('--drop-cache', nargs='*', metavar='PREFIX', help='forget cached answers of these registry calls first, e.g. semantic_scholar_lookup_batch')
    ap.add_argument('--cache-only', action='store_true', help='never call the network; misses act as unreachable')
    ap.add_argument('--seed', type=int, default=13)
    run(ap.parse_args(argv))
    return 0


if __name__ == '__main__':
    sys.exit(main())
