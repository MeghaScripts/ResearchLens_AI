"""Small pure-Python statistics and model fitting for the evaluation (no numpy / scikit-learn needed).

  auc                ranking quality of a score against 0/1 labels (Mann-Whitney, ties handled)
  spearman           rank correlation (average ranks for ties)
  weighted_kappa     quadratic-weighted Cohen's kappa between two raters' ordinal ratings
  logistic_fit       logistic regression by batch gradient descent, optional non-negative weights
  ridge_fit          ridge regression, solved exactly (intercept not penalised)
"""
import math


def auc(scores, labels):
    """P(score of a random positive > score of a random negative), ties count 1/2. 0.5 when one class is absent."""
    pos = [s for s, l in zip(scores, labels) if l]
    neg = [s for s, l in zip(scores, labels) if not l]
    if not pos or not neg:
        return 0.5
    ranked = sorted([(s, 1) for s in pos] + [(s, 0) for s in neg], key=lambda t: t[0])
    ranks, i = {}, 0
    while i < len(ranked):
        j = i
        while j + 1 < len(ranked) and ranked[j + 1][0] == ranked[i][0]:
            j += 1
        ranks[(i, j)] = (i + j) / 2 + 1
        i = j + 1
    pos_rank_sum, idx = 0.0, 0
    for (lo, hi), r in ranks.items():
        pos_rank_sum += r * sum(1 for k in range(lo, hi + 1) if ranked[k][1] == 1)
    return (pos_rank_sum - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def _avg_ranks(values):
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(x, y):
    if len(x) != len(y) or len(x) < 3:
        return 0.0
    rx, ry = _avg_ranks(x), _avg_ranks(y)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else 0.0


def weighted_kappa(a, b, categories=(1, 2, 3, 4, 5)):
    """Quadratic-weighted Cohen's kappa for two raters over the same items. 1 = perfect, 0 = chance."""
    n = len(a)
    if n == 0 or n != len(b):
        return 0.0
    k = len(categories)
    idx = {c: i for i, c in enumerate(categories)}
    obs = [[0.0] * k for _ in range(k)]
    for x, y in zip(a, b):
        obs[idx[x]][idx[y]] += 1
    row = [sum(obs[i]) for i in range(k)]
    col = [sum(obs[i][j] for i in range(k)) for j in range(k)]
    num = den = 0.0
    for i in range(k):
        for j in range(k):
            w = ((i - j) ** 2) / ((k - 1) ** 2)
            num += w * obs[i][j]
            den += w * row[i] * col[j] / n
    return 1.0 - num / den if den else 0.0


def logistic_fit(X, y, l2=0.01, lr=0.5, iters=1500, nonneg=False):
    """-> (weights, bias). With nonneg=True weights are projected to >= 0 each step (interpretable blend)."""
    if not X:
        return [], 0.0
    n, d = len(X), len(X[0])
    w, b = [0.0] * d, 0.0
    for _ in range(iters):
        gw, gb = [0.0] * d, 0.0
        for xi, yi in zip(X, y):
            z = b + sum(wj * xj for wj, xj in zip(w, xi))
            p = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))
            e = p - yi
            gb += e
            for j in range(d):
                gw[j] += e * xi[j]
        for j in range(d):
            w[j] -= lr * (gw[j] / n + l2 * w[j])
            if nonneg and w[j] < 0:
                w[j] = 0.0
        b -= lr * gb / n
    return w, b


def _solve(A, rhs):
    """Gauss-Jordan with partial pivoting for a small dense system."""
    n = len(A)
    M = [row[:] + [rhs[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[p][c]) < 1e-12:
            continue
        M[c], M[p] = M[p], M[c]
        pivot = M[c][c]
        M[c] = [v / pivot for v in M[c]]
        for r in range(n):
            if r != c and M[r][c]:
                f = M[r][c]
                M[r] = [v - f * cv for v, cv in zip(M[r], M[c])]
    return [M[i][n] for i in range(n)]


def ridge_fit(X, y, lam=1.0):
    """-> (weights, bias). Columns and target are centred so the intercept is not shrunk."""
    if not X:
        return [], 0.0
    n, d = len(X), len(X[0])
    xm = [sum(row[j] for row in X) / n for j in range(d)]
    ym = sum(y) / n
    Xc = [[row[j] - xm[j] for j in range(d)] for row in X]
    yc = [v - ym for v in y]
    A = [[sum(Xc[r][i] * Xc[r][j] for r in range(n)) + (lam if i == j else 0.0) for j in range(d)] for i in range(d)]
    rhs = [sum(Xc[r][i] * yc[r] for r in range(n)) for i in range(d)]
    w = _solve(A, rhs)
    return w, ym - sum(wj * mj for wj, mj in zip(w, xm))
