"""Pure-Python metrics for the verification evaluation (no numpy, no network).

Ground truth is two-way: a paper is either something the system should ACCEPT (a real, relevant,
non-retracted record) or REJECT (fabricated, altered, retracted or off-topic). The system answers
three-way: accept / reject / review (abstain). So the confusion matrix has an extra `review` column,
and two numbers are reported side by side:

  coverage          share of papers the system decided (accept or reject) instead of sending to review
  decisive accuracy accuracy over only those decided papers

95% on decisive verdicts at a stated coverage is an honest claim. 95% "on everything" is not, because
an abstention is neither right nor wrong.
"""
import math

LABELS = ('accept', 'reject')
PREDICTIONS = ('accept', 'reject', 'review')


def wilson_interval(successes, n, z=1.96):
    """95% Wilson score interval for a proportion -> (low, high); (0, 0) when n == 0."""
    if n <= 0:
        return 0.0, 0.0
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def confusion(rows):
    """rows: iterable of (label, prediction). -> {label: {prediction: count}} with all cells present."""
    m = {l: {p: 0 for p in PREDICTIONS} for l in LABELS}
    for label, pred in rows:
        if label in m and pred in m[label]:
            m[label][pred] += 1
    return m


def summarise(rows):
    """Headline numbers for one set of (label, prediction) rows."""
    rows = list(rows)
    cm = confusion(rows)
    n = len(rows)
    decided = [(l, p) for l, p in rows if p in LABELS]
    correct = sum(1 for l, p in decided if l == p)
    out = {
        'n': n,
        'decided': len(decided),
        'review': n - len(decided),
        'coverage': (len(decided) / n) if n else 0.0,
        'decisive_accuracy': (correct / len(decided)) if decided else 0.0,
        'decisive_accuracy_ci95': wilson_interval(correct, len(decided)),
        # strict accuracy counts every abstention as a miss: the number to quote if someone asks about "everything"
        'strict_accuracy': (correct / n) if n else 0.0,
        'confusion': cm,
        'per_class': {},
    }
    for cls in LABELS:
        tp = sum(1 for l, p in decided if l == cls and p == cls)
        fp = sum(1 for l, p in decided if l != cls and p == cls)
        fn_decided = sum(1 for l, p in decided if l == cls and p != cls)
        fn_all = fn_decided + cm[cls]['review']
        out['per_class'][cls] = {
            'precision': tp / (tp + fp) if (tp + fp) else 0.0,
            'recall_decided': tp / (tp + fn_decided) if (tp + fn_decided) else 0.0,
            'recall_all': tp / (tp + fn_all) if (tp + fn_all) else 0.0,
            'support': sum(cm[cls].values()),
        }
    return out


def by_category(records):
    """records: dicts with 'category', 'label', 'prediction'. -> {category: summarise()} so the report
    shows WHICH kind of fake (wrong DOI, swapped authors, retracted...) gets through."""
    groups = {}
    for r in records:
        groups.setdefault(r['category'], []).append((r['label'], r['prediction']))
    return {c: summarise(rows) for c, rows in sorted(groups.items())}


def format_report(title, summary):
    lo, hi = summary['decisive_accuracy_ci95']
    cm = summary['confusion']
    lines = [
        f'== {title} ==',
        f"n={summary['n']}  decided={summary['decided']}  sent to review={summary['review']}",
        f"coverage            {summary['coverage']:.1%}",
        f"decisive accuracy   {summary['decisive_accuracy']:.1%}  (95% CI {lo:.1%} - {hi:.1%})",
        f"strict accuracy     {summary['strict_accuracy']:.1%}  (abstentions counted as misses)",
        '',
        'confusion matrix (rows = truth, columns = system)',
        f"{'':10}{'accept':>8}{'reject':>8}{'review':>8}",
    ]
    for label in LABELS:
        lines.append(f"{label:10}{cm[label]['accept']:>8}{cm[label]['reject']:>8}{cm[label]['review']:>8}")
    lines.append('')
    for cls, v in summary['per_class'].items():
        lines.append(f"{cls:8} precision {v['precision']:.1%}  recall(decided) {v['recall_decided']:.1%}  "
                     f"recall(all) {v['recall_all']:.1%}  n={v['support']}")
    return '\n'.join(lines)
