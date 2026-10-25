"""Evaluate the three verification levels separately.

    python -m eval.run_levels                                   # level 1 (authenticity) + level 2 (relevance)
    python -m eval.run_levels --make-quality-template 100       # blank sheet for human quality ratings
    python -m eval.run_levels --ratings eval/quality_ratings.csv   # + level 3 (quality) vs the ratings
    python -m eval.run_levels --strict --cv 5 --out eval/levels_strict.json

(run from backend/; same cache as eval.run_eval, so nothing is re-fetched.)

Level 1  Does the record check out against the registries? Off-topic papers are authentic, so they are
         left out; the relevance cutoff is switched off so only authenticity decides. Pooled out-of-fold
         accuracy on decided papers, plus coverage.
Level 2  Is the paper on the topic? 900-ish (paper, topic) pairs from the same labelled set.
Level 3  Does the quality score agree with human raters? Needs ratings you collect; reported as a
         correlation, not an accuracy.
"""
import argparse
import json
import os
import sys

import paper_verification as pv
from eval import cache as registry_cache
from eval import metrics, quality_eval, relevance_eval
from eval.build_eval_set import load_jsonl
from eval.run_eval import cv_evaluate, decide, verify_once, _score


def level1(records, k, target, seed):
    pairs = [p for p in verify_once(records) if p[0]['category'] != 'off_topic']
    preds, chosen = cv_evaluate(pairs, k=k, target=target, auth_grid=(60, 65, 70, 75, 80, 85, 90),
                                rel_grid=(0,), seed=seed)
    s = _score(preds)
    print(metrics.format_report(f'LEVEL 1 AUTHENTICITY ({k}-fold pooled out-of-fold, n={len(preds)}; off-topic excluded)', s))
    by_cat = metrics.by_category(preds)
    print('\nPer category: decided / n  and decisive accuracy')
    for cat, c in by_cat.items():
        print(f"  {cat:16} {c['decided']:>3}/{c['n']:<3}  {c['decisive_accuracy']:.0%}")
    default = _score(decide(pairs, rel_min=0))
    print(f"\nAt the shipped authenticity cutoff ({pv.AUTHENTICITY_REJECT_THRESHOLD:.0f}), no tuning: "
          f"coverage {default['coverage']:.1%}, decisive accuracy {default['decisive_accuracy']:.1%}")
    return {'pooled': s, 'by_category': by_cat, 'thresholds_per_fold': chosen, 'defaults': default}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--data', default='eval/labelled.jsonl')
    ap.add_argument('--cache', default='eval/registry_cache.json')
    ap.add_argument('--out', default='eval/levels.json')
    ap.add_argument('--cv', type=int, default=5)
    ap.add_argument('--target', type=float, default=0.95)
    ap.add_argument('--seed', type=int, default=13)
    ap.add_argument('--strict', action='store_true')
    ap.add_argument('--cache-only', action='store_true')
    ap.add_argument('--drop-cache', nargs='*', metavar='PREFIX')
    ap.add_argument('--ratings', help='CSV of human quality ratings (see eval/quality_eval.py)')
    ap.add_argument('--make-quality-template', type=int, metavar='N', help='write a blank rating sheet with N papers')
    ap.add_argument('--quality-sheet', default='eval/quality_ratings.csv')
    a = ap.parse_args(argv)

    records = load_jsonl(a.data)
    if a.make_quality_template:
        n = quality_eval.make_template(records, a.make_quality_template, a.quality_sheet, a.seed)
        print(f'[levels] wrote {n} papers to {a.quality_sheet}. Give it to 3 raters (columns rater1..rater3, 1-5), '
              f'then run with --ratings {a.quality_sheet}')
        return 0

    handle = registry_cache.install(a.cache, cache_only=a.cache_only, drop_prefixes=a.drop_cache or ())
    if a.strict:
        pv.STRICT_FIELD_REJECT = True
    try:
        report = {'strict': bool(a.strict), 'n_records': len(records)}
        report['level1_authenticity'] = level1(records, a.cv, a.target, a.seed)

        real, topics = relevance_eval.real_papers(records)
        if real and len(topics) >= 2:
            rows = relevance_eval.score_pairs(real, topics)
            rep = relevance_eval.evaluate(rows, real, k=a.cv, seed=a.seed)
            print('\n' + relevance_eval.format_report(rep))
            report['level2_relevance'] = rep
        else:
            print('\n[levels] level 2 needs real papers from at least 2 topics')

        if a.ratings:
            rows = quality_eval.load_ratings(a.ratings)
            q = quality_eval.evaluate(rows, records, k=a.cv, seed=a.seed)
            print('\n' + quality_eval.format_report(q))
            report['level3_quality'] = q
        else:
            print('\n[levels] level 3 (quality): no ratings given. Run with --make-quality-template 100, have 3 people '
                  'rate the sheet, then pass --ratings. Quality is reported as a correlation with raters, not an accuracy.')

        print(f"\n[cache] hits={handle.stats['hits']} misses={handle.stats['misses']} stored={handle.stats['stored']}")
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, default=str)
        print(f'[levels] wrote {a.out}')
    finally:
        handle.save()
        handle.restore()
    return 0


if __name__ == '__main__':
    sys.exit(main())
