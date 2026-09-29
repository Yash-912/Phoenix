"""Deterministic evidence-weighted scoring (Slice 2.4, Step B).

The LLM may DESCRIBE hypotheses (see schemas.py). Only this module decides
how much to BELIEVE them — pure code, no model calls, fully unit-testable.

Rule (V1, deliberately simple):
  score = 0.4*prom + 0.3*loki + 0.3*docker + agreement_bonus - contradiction_penalty
  clamped to [0, 1], with a full breakdown dict for the audit trail.
"""

from __future__ import annotations

import json

from phoenix.graph.schemas import Hypothesis

PROM_WEIGHT = 0.4
LOKI_WEIGHT = 0.3
DOCKER_WEIGHT = 0.3
AGREEMENT_BONUS = 0.2
CONTRADICTION_PENALTY = 0.3

SOURCE_PROM = "query_prometheus"
SOURCE_LOKI = "query_loki"
SOURCE_DOCKER = "get_container_state"

CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "crash": ["down", "exit", "dead", "oom", "crash", "restart", "servicedown", "up==0"],
    "overload": ["latency", "slow", "p95", "highlatency", "cpu", "memory", "overload", "5xx", "error rate", "higherrorrate"],
    "deploy": ["deploy", "version", "release", "image", "v18", "v17", "rollout"],
    "config": ["config", "pool", "timeout", "connection", "env var", "setting"],
    "network": ["network", "connection refused", "dns", "unreachable", "timeout"],
    "unknown": [],
}


def _is_usable(evidence_item: dict) -> bool:
    """Usable = collection succeeded (not our {'status':'error'} envelope)."""
    raw = evidence_item.get("raw_data", {})
    if not isinstance(raw, dict):
        return True
    return raw.get("status") != "error"


def _blob(evidence_item: dict) -> str:
    """Lowercased JSON text of summary + raw_data for keyword matching."""
    try:
        return json.dumps(
            {"summary": evidence_item.get("summary", ""), "raw_data": evidence_item.get("raw_data", {})},
            default=str,
        ).lower()
    except (TypeError, ValueError):
        return str(evidence_item.get("summary", "")).lower()


def _supports(blob: str, category: str) -> bool:
    keywords = CATEGORY_KEYWORDS.get(category, [])
    if not keywords:
        return False
    return any(kw in blob for kw in keywords)


def score_hypothesis(evidence: list[dict], hypothesis: Hypothesis) -> tuple[float, dict]:
    """Score one hypothesis against all evidence. Returns (score, breakdown)."""
    usable = [e for e in evidence if _is_usable(e)]
    blobs_by_source: dict[str, list[str]] = {SOURCE_PROM: [], SOURCE_LOKI: [], SOURCE_DOCKER: []}
    for item in usable:
        src = item.get("source", "")
        if src in blobs_by_source:
            blobs_by_source[src].append(_blob(item))

    has_prom = 1 if any(_supports(b, hypothesis.category) for b in blobs_by_source[SOURCE_PROM]) else 0
    has_loki = 1 if any(_supports(b, hypothesis.category) for b in blobs_by_source[SOURCE_LOKI]) else 0
    has_docker = 1 if any(_supports(b, hypothesis.category) for b in blobs_by_source[SOURCE_DOCKER]) else 0

    sources_supporting = has_prom + has_loki + has_docker
    agreement_bonus = AGREEMENT_BONUS if sources_supporting >= 2 else 0.0

    # Contradiction: we looked thoroughly (usable evidence from >=2 distinct
    # sources) but NOTHING matches this hypothesis -> penalize the guess.
    distinct_sources_with_data = sum(1 for blobs in blobs_by_source.values() if blobs)
    contradiction_penalty = (
        CONTRADICTION_PENALTY
        if (sources_supporting == 0 and distinct_sources_with_data >= 2 and hypothesis.category != "unknown")
        else 0.0
    )

    raw_score = (
        PROM_WEIGHT * has_prom
        + LOKI_WEIGHT * has_loki
        + DOCKER_WEIGHT * has_docker
        + agreement_bonus
        - contradiction_penalty
    )
    score = round(max(0.0, min(1.0, raw_score)), 4)

    breakdown = {
        "has_prometheus_signal": has_prom,
        "has_loki_signal": has_loki,
        "has_docker_signal": has_docker,
        "sources_supporting": sources_supporting,
        "agreement_bonus": agreement_bonus,
        "contradiction_penalty": contradiction_penalty,
        "weights": {"prometheus": PROM_WEIGHT, "loki": LOKI_WEIGHT, "docker": DOCKER_WEIGHT},
        "category": hypothesis.category,
    }
    return score, breakdown


def score_all(evidence: list[dict], hypotheses: list[Hypothesis]) -> list[tuple[Hypothesis, float, dict]]:
    """Score every hypothesis, sorted best-first. Empty input -> []."""
    scored = [(h, *score_hypothesis(evidence, h)) for h in hypotheses]
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored


def top_confidence(evidence: list[dict], hypotheses: list[Hypothesis]) -> float:
    """Overall confidence = best hypothesis score, or 0.0 if none."""
    scored = score_all(evidence, hypotheses)
    return scored[0][1] if scored else 0.0
