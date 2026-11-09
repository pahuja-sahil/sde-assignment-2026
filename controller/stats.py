"""Deterministic statistical tests used by detection and promotion.

An LLM is never consulted for a winner. Every function takes counts and returns
numbers; the caller applies pre-registered thresholds.
"""
from __future__ import annotations

import math
import random


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def two_proportion_z(k1: int, n1: int, k2: int, n2: int) -> tuple[float, float, float]:
    """Returns (difference, z, two-sided p) for p1 - p2."""
    if n1 <= 0 or n2 <= 0:
        return 0.0, 0.0, 1.0
    p1, p2 = k1 / n1, k2 / n2
    diff = p1 - p2
    pooled = (k1 + k2) / (n1 + n2)
    se = math.sqrt(max(pooled * (1 - pooled) * (1 / n1 + 1 / n2), 1e-12))
    z = diff / se
    p = math.erfc(abs(z) / math.sqrt(2))
    return diff, z, p


def risk_diff_ci(k1: int, n1: int, k2: int, n2: int, z: float = 1.96) -> tuple[float, float, float]:
    if n1 <= 0 or n2 <= 0:
        return 0.0, -1.0, 1.0
    p1, p2 = k1 / n1, k2 / n2
    diff = p1 - p2
    se = math.sqrt(max(p1 * (1 - p1) / n1 + p2 * (1 - p2) / n2, 1e-12))
    return diff, diff - z * se, diff + z * se


def relative_lift(k: int, n: int, base_k: int, base_n: int) -> float:
    if n <= 0 or base_n <= 0:
        return 0.0
    base = base_k / base_n
    if base <= 1e-12:
        return 0.0
    return (k / n) / base - 1.0


def beta_params(k: int, n: int) -> tuple[float, float]:
    return max(0.5, k + 0.5), max(0.5, n - k + 0.5)


def beta_mean(k: int, n: int) -> float:
    a, b = beta_params(k, n)
    return a / (a + b)


def beta_prob_better(k1: int, n1: int, k2: int, n2: int, seed: str, draws: int = 4000) -> float:
    """Deterministic Monte Carlo P(arm1 > arm2) with fixed seed."""
    a1, b1 = beta_params(k1, n1)
    a2, b2 = beta_params(k2, n2)
    rng = random.Random(seed)
    wins = 0
    for _ in range(draws):
        if rng.betavariate(a1, b1) > rng.betavariate(a2, b2):
            wins += 1
    return wins / draws


def benjamini_hochberg(pvalues: list[float], q: float = 0.10) -> list[bool]:
    m = len(pvalues)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvalues[i])
    passed = [False] * m
    cutoff = 0
    for rank, idx in enumerate(order, start=1):
        if pvalues[idx] <= q * rank / m:
            cutoff = rank
    for rank, idx in enumerate(order, start=1):
        passed[idx] = rank <= cutoff
    return passed


def smoothed_rate(k: int, n: int, prior_k: float = 1.0, prior_n: float = 20.0) -> float:
    return (k + prior_k) / (n + prior_n)
