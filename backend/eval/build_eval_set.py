"""Build the labelled evaluation set for paper verification.

    python -m eval.build_eval_set --n-real 150 --n-bad 150 --out eval/labelled.jsonl

(run from backend/). Needs network access to api.openalex.org - run it on your own machine; set
OPENALEX_API_KEY and OPENALEX_MAILTO to avoid throttling.

Positives (label 'accept'): real, non-retracted OpenAlex works that have a DOI and an abstract.
Negatives (label 'reject'), made from the real ones with a fixed random seed so the file is reproducible:

  wrong_doi        real title/authors/year but ANOTHER real paper's DOI   (the recycled-DOI case)
  fake_doi         real metadata with an invented DOI that resolves nowhere
  altered_title    real DOI, but the title is scrambled/edited
  swapped_authors  real DOI + title, authors taken from a different paper
  invented         fully fabricated title, authors, venue and DOI
  retracted        real works OpenAlex flags is_retracted:true
  off_topic        a real paper from another field, filed under this topic

`swapped_authors` is a single-field contradiction, which the verifier deliberately downgrades to
PARTIALLY_VERIFIED (review) rather than rejecting, so expect it to land in the review column.

Everything except `fetch_*` is pure and unit-tested offline (tests/test_eval_smoke.py).
"""
import argparse
import json
import os
import random
import re
import sys
import time

DEFAULT_TOPICS = [
    'federated learning', 'graph neural networks', 'protein structure prediction',
    'speech emotion recognition', 'remote sensing image segmentation', 'crispr gene editing',
]
BAD_MIX = {            # share of the negatives per category
    'wrong_doi': 0.20, 'fake_doi': 0.15, 'altered_title': 0.15, 'swapped_authors': 0.10,
    'invented': 0.15, 'retracted': 0.15, 'off_topic': 0.10,
}
_FAKE_WORDS = ('adaptive', 'quantum', 'neural', 'latent', 'robust', 'stochastic', 'hierarchical', 'sparse',
               'federated', 'contrastive', 'bayesian', 'spectral', 'cross-modal', 'differentiable')
_FAKE_NOUNS = ('framework', 'manifold', 'operator', 'lattice', 'estimator', 'transformer', 'ensemble',
               'diffusion', 'kernel', 'protocol')
_FAKE_SURNAMES = ('Valdemar', 'Okonkwo-Reyes', 'Thackeray', 'Brunelleschi', 'Nakamura-Holt', 'Ferreira-Singh')


# ───────────────────────── pure helpers ─────────────────────────
def abstract_from_inverted_index(inv):
    if not inv:
        return ''
    pos = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return ' '.join(pos[i] for i in sorted(pos))


def to_paper(work, topic):
    """OpenAlex work JSON -> the paper dict shape verify_papers consumes."""
    doi = (work.get('doi') or '').replace('https://doi.org/', '').strip()
    loc = work.get('primary_location') or {}
    src = (loc.get('source') or {}).get('display_name') or ''
    return {
        'title': (work.get('title') or '').strip(),
        'authors': [a['author']['display_name'] for a in (work.get('authorships') or [])
                    if a.get('author') and a['author'].get('display_name')][:10],
        'year': work.get('publication_year'),
        'doi': doi,
        'venue': src,
        'abstract': abstract_from_inverted_index(work.get('abstract_inverted_index')),
        'citation_count': work.get('cited_by_count'),
        'source': 'OpenAlex',
        'topic': topic,
        'is_retracted_truth': bool(work.get('is_retracted')),
    }


def _record(i, category, label, paper):
    r = dict(paper)
    r.pop('is_retracted_truth', None)
    r.update({'id': f'e{i:04d}', 'category': category, 'label': label})
    return r


def _fake_doi(rng):
    return f'10.{rng.randint(1000, 9999)}/{rng.choice("abcdefghjkmnpqrstuvwxyz")}{rng.randint(10**6, 10**7 - 1)}'


def _alter_title(title, rng):
    words = title.split()
    if len(words) < 4:
        return title + ' ' + rng.choice(_FAKE_WORDS)
    rng.shuffle(words)
    words[0] = rng.choice(_FAKE_WORDS).capitalize()
    return ' '.join(words)


def _invented(topic, rng):
    title = (f'{rng.choice(_FAKE_WORDS).capitalize()} {rng.choice(_FAKE_NOUNS)} for '
             f'{topic} with {rng.choice(_FAKE_WORDS)} {rng.choice(_FAKE_NOUNS)}')
    return {
        'title': title,
        'authors': [f'{rng.choice("ABCDEFGHJKLMNPRST")}. {rng.choice(_FAKE_SURNAMES)}' for _ in range(3)],
        'year': rng.randint(2019, 2025),
        'doi': _fake_doi(rng),
        'venue': f'International Journal of {rng.choice(_FAKE_WORDS).capitalize()} {rng.choice(_FAKE_NOUNS).capitalize()}s',
        'abstract': f'We present a {rng.choice(_FAKE_WORDS)} approach to {topic} and report improvements.',
        'source': 'OpenAlex',
        'topic': topic,
    }


def make_dataset(real, retracted, n_bad, seed=13):
    """real/retracted: lists of paper dicts (see to_paper). -> list of labelled records.

    Deterministic for a given seed and input order. Every real paper is kept as a positive; negatives
    are `n_bad` records spread over BAD_MIX."""
    rng = random.Random(seed)
    records, n = [], 0

    def add(category, label, paper):
        nonlocal n
        n += 1
        records.append(_record(n, category, label, paper))

    for p in real:
        add('real', 'accept', p)

    counts = {c: int(round(n_bad * share)) for c, share in BAD_MIX.items()}
    counts['invented'] += n_bad - sum(counts.values())              # absorb rounding in one bucket
    pool = [p for p in real if p.get('doi') and p.get('authors') and p.get('title')]
    if len(pool) < 2:
        raise ValueError('need at least 2 real papers with DOI, title and authors to build negatives')

    def pick(k):
        return [rng.choice(pool) for _ in range(k)]

    for base in pick(counts['wrong_doi']):
        other = rng.choice([q for q in pool if q['doi'] != base['doi']])
        add('wrong_doi', 'reject', dict(base, doi=other['doi']))
    for base in pick(counts['fake_doi']):
        add('fake_doi', 'reject', dict(base, doi=_fake_doi(rng)))
    for base in pick(counts['altered_title']):
        add('altered_title', 'reject', dict(base, title=_alter_title(base['title'], rng)))
    for base in pick(counts['swapped_authors']):
        other = rng.choice([q for q in pool if q['authors'] != base['authors']])
        add('swapped_authors', 'reject', dict(base, authors=list(other['authors'])))
    topics = sorted({p['topic'] for p in pool})
    for _ in range(counts['invented']):
        add('invented', 'reject', _invented(rng.choice(topics), rng))
    ret = list(retracted)
    rng.shuffle(ret)
    for p in (ret[:counts['retracted']] if ret else []):
        add('retracted', 'reject', p)
    for _ in range(counts['off_topic']):
        base = rng.choice(pool)
        other_topic = rng.choice([t for t in topics if t != base['topic']] or topics)
        add('off_topic', 'reject', dict(base, topic=other_topic))
    return records


def split_train_test(records, test_fraction=0.30, seed=13):
    """Stratified by category so both splits contain every kind of fake. -> (train, test)."""
    rng = random.Random(seed)
    by_cat = {}
    for r in records:
        by_cat.setdefault(r['category'], []).append(r)
    train, test = [], []
    for cat in sorted(by_cat):
        items = by_cat[cat][:]
        rng.shuffle(items)
        k = max(1, int(round(len(items) * test_fraction))) if len(items) > 1 else 0
        test.extend(items[:k])
        train.extend(items[k:])
    return train, test


def save_jsonl(records, path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')


def load_jsonl(path):
    with open(path, encoding='utf-8') as f:
        return [json.loads(line) for line in f if line.strip()]


# ───────────────────────── network (run on your machine) ─────────────────────────
_SELECT = 'id,doi,title,publication_year,authorships,primary_location,abstract_inverted_index,cited_by_count,is_retracted'


def _get(params):
    import requests
    params = dict(params, select=_SELECT)
    mailto = os.environ.get('OPENALEX_MAILTO') or os.environ.get('CROSSREF_MAILTO')
    if mailto:
        params['mailto'] = mailto
    if os.environ.get('OPENALEX_API_KEY'):
        params['api_key'] = os.environ['OPENALEX_API_KEY']
    for attempt in range(4):
        r = requests.get('https://api.openalex.org/works', params=params, timeout=30)
        if r.status_code == 200:
            return r.json().get('results', [])
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f'OpenAlex request failed: HTTP {r.status_code}')


def fetch_real(topic, n, seed=13):
    flt = ('title_and_abstract.search:%s,has_doi:true,has_abstract:true,is_retracted:false,'
           'type:article,from_publication_date:2018-01-01' % topic)
    works = _get({'filter': flt, 'per-page': min(200, n * 2), 'sample': min(200, n * 2), 'seed': seed})
    out = [to_paper(w, topic) for w in works]
    return [p for p in out if p['title'] and p['doi'] and p['authors'] and p['abstract']][:n]


def fetch_retracted(topic, n):
    flt = 'title_and_abstract.search:%s,has_doi:true,is_retracted:true' % topic
    out = [to_paper(w, topic) for w in _get({'filter': flt, 'per-page': min(200, n * 2)})]
    return [p for p in out if p['title'] and p['doi']][:n]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--n-real', type=int, default=150)
    ap.add_argument('--n-bad', type=int, default=150)
    ap.add_argument('--topics', nargs='*', default=DEFAULT_TOPICS)
    ap.add_argument('--seed', type=int, default=13)
    ap.add_argument('--out', default='eval/labelled.jsonl')
    args = ap.parse_args(argv)

    per_topic = -(-args.n_real // len(args.topics))
    real, retracted = [], []
    for t in args.topics:
        print(f'[build] {t}: fetching real + retracted works')
        real.extend(fetch_real(t, per_topic, args.seed))
        retracted.extend(fetch_retracted(t, max(3, int(args.n_bad * BAD_MIX['retracted'] / len(args.topics)) + 2)))
        time.sleep(0.5)
    real = real[:args.n_real]
    records = make_dataset(real, retracted, args.n_bad, seed=args.seed)
    save_jsonl(records, args.out)
    cats = {}
    for r in records:
        cats[r['category']] = cats.get(r['category'], 0) + 1
    print(f'[build] wrote {len(records)} records to {args.out}: {cats}')
    if cats.get('retracted', 0) < int(args.n_bad * BAD_MIX['retracted']):
        print('[build] WARNING: fewer retracted papers than planned; rerun with more topics')
    return 0


if __name__ == '__main__':
    sys.exit(main())
