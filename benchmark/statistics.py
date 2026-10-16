"""Seeded paired statistics; percentile intervals resample whole prompt pairs."""
import math
import random
from statistics import mean


def percentile(values, p):
    values = sorted(values)
    if not values:
        return None
    pos = (len(values) - 1) * p
    low = math.floor(pos)
    high = math.ceil(pos)
    return values[low] + (values[high] - values[low]) * (pos - low)


def bootstrap_ci(values, seed=13, resamples=10000):
    if not values:
        return [None, None]
    rng = random.Random(seed)
    means = [mean(rng.choices(values, k=len(values))) for _ in range(resamples)]
    return [percentile(means, .025), percentile(means, .975)]


def ranks(values):
    ordered = sorted(range(len(values)), key=values.__getitem__)
    result = [0.0] * len(values)
    i = 0
    while i < len(ordered):
        j = i + 1
        while j < len(ordered) and values[ordered[j]] == values[ordered[i]]:
            j += 1
        for index in ordered[i:j]:
            result[index] = (i + j - 1) / 2 + 1
        i = j
    return result


def spearman(x, y):
    if len(x) != len(y) or len(x) < 3:
        return None
    x, y = ranks(x), ranks(y)
    mx, my = mean(x), mean(y)
    denominator = math.sqrt(sum((v - mx) ** 2 for v in x) * sum((v - my) ** 2 for v in y))
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / denominator if denominator else None


def correlation_ci(x, y, seed=13, resamples=10000):
    rng = random.Random(seed)
    values = []
    for _ in range(resamples):
        indices = rng.choices(range(len(x)), k=len(x))
        value = spearman([x[i] for i in indices], [y[i] for i in indices])
        if value is not None:
            values.append(value)
    if len(values) < .95 * resamples:
        return [None, None]
    return [percentile(values, .025), percentile(values, .975)]


def non_inferiority(a, b, completed_a=None, completed_b=None, seed=13, resamples=10000):
    if len(a) != len(b) or not a:
        return {"supported": False, "reason": "missing matched quality pairs"}
    ca = completed_a if completed_a is not None else [True] * len(a)
    cb = completed_b if completed_b is not None else [True] * len(b)
    differences = [(vb if good_b else 1) - (va if good_a else 1)
                   for va, vb, good_a, good_b in zip(a, b, ca, cb)]
    ci = bootstrap_ci(differences, seed, resamples)
    sensitivity = [vb - va for va, vb, ga, gb in zip(a, b, ca, cb) if ga and gb]
    return dict(mean_difference=mean(differences), ci=ci, margin=-.3,
                supported=ci[0] > -.3, n=len(a),
                sensitivity_completed_only=dict(n=len(sensitivity),
                                                ci=bootstrap_ci(sensitivity, seed, resamples)))


def paired_savings(a, b, seed=13, resamples=10000):
    if len(a) != len(b) or not a or sum(a) <= 0:
        return dict(ratio=None, ci=[None,None])
    rng = random.Random(seed)
    ratios = []
    for _ in range(resamples):
        indices = rng.choices(range(len(a)),k=len(a))
        baseline = sum(a[i] for i in indices)
        if baseline > 0:
            ratios.append(1 - sum(b[i] for i in indices)/baseline)
    return dict(ratio=1-sum(b)/sum(a), ci=[percentile(ratios,.025),percentile(ratios,.975)])
