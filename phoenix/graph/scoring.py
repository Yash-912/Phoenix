"""Deterministic evidence-weighted scoring (Slice 2.4, Step B).

The LLM may DESCRIBE hypotheses (see schemas.py). Only this module decides
how much to BELIEVE them — pure code, no model calls, fully unit-testable.

Rule (V1, deliberately simple):
  score = 0.4*prom + 0.3*loki + 0.3*docker + 0.15*health + 0.15*deploy
          + agreement_bonus - contradiction_penalty
  clamped to [0, 1], with a full breakdown dict for the audit trail.
"""

from __future__ import annotations

from phoenix.graph.schemas import Hypothesis

PROM_WEIGHT = 0.4
LOKI_WEIGHT = 0.3
DOCKER_WEIGHT = 0.3
HEALTH_WEIGHT = 0.15
DEPLOYMENTS_WEIGHT = 0.15
AGREEMENT_BONUS = 0.2
CONTRADICTION_PENALTY = 0.3

SOURCE_PROM = "query_prometheus"
SOURCE_LOKI = "query_loki"
SOURCE_DOCKER = "get_container_state"
SOURCE_HEALTH = "inspect_health"
SOURCE_DEPLOYMENTS = "get_recent_deployments"

SOURCE_WEIGHTS: dict[str, float] = {
    SOURCE_PROM: PROM_WEIGHT,
    SOURCE_LOKI: LOKI_WEIGHT,
    SOURCE_DOCKER: DOCKER_WEIGHT,
    SOURCE_HEALTH: HEALTH_WEIGHT,
    SOURCE_DEPLOYMENTS: DEPLOYMENTS_WEIGHT,
}

SOURCE_SIGNAL_KEYS: dict[str, str] = {
    SOURCE_PROM: "has_prometheus_signal",
    SOURCE_LOKI: "has_loki_signal",
    SOURCE_DOCKER: "has_docker_signal",
    SOURCE_HEALTH: "has_health_signal",
    SOURCE_DEPLOYMENTS: "has_deploy_signal",
}

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


def _content_summary(evidence_item: dict) -> str:
    """summary with its leading ``source(arguments)`` label removed."""
    summary = str(evidence_item.get("summary", ""))
    source = str(evidence_item.get("source", ""))
    label = f"{source}("
    if source and summary.startswith(label) and summary.endswith(")"):
        return summary[len(label) : -1]
    return summary


def _content_values(payload) -> list[str]:
    """Scalar leaf values of a payload, field names discarded."""
    if isinstance(payload, dict):
        return [text for value in payload.values() for text in _content_values(value)]
    if isinstance(payload, (list, tuple)):
        return [text for value in payload for text in _content_values(value)]
    return [str(payload)]


def _blob(evidence_item: dict) -> str:
    """Lowercased content of one evidence item for keyword matching: the
    tool-name-stripped summary plus raw_data's leaf values.

    Labels are excluded on purpose — the tool name and raw_data's field names
    appear whether or not the tool found anything, so letting them match would
    score an observer ACTION as evidence. A label must never satisfy a category
    keyword on its own; only returned data can.
    """
    summary = _content_summary(evidence_item)
    try:
        values = _content_values(evidence_item.get("raw_data", {}))
    except RecursionError:
        values = []
    return "\n".join([summary, *values]).lower()


def _supports(blob: str, category: str) -> bool:
    keywords = CATEGORY_KEYWORDS.get(category, [])
    if not keywords:
        return False
    return any(kw in blob for kw in keywords)


def score_hypothesis(evidence: list[dict], hypothesis: Hypothesis) -> tuple[float, dict]:
    """Score one hypothesis against all evidence. Returns (score, breakdown)."""
    usable = [e for e in evidence if _is_usable(e)]
    blobs_by_source: dict[str, list[str]] = {src: [] for src in SOURCE_WEIGHTS}
    for item in usable:
        src = item.get("source", "")
        if src in blobs_by_source:
            blobs_by_source[src].append(_blob(item))

    signals = {
        src: 1 if any(_supports(b, hypothesis.category) for b in blobs) else 0
        for src, blobs in blobs_by_source.items()
    }

    sources_supporting = sum(signals.values())
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
        sum(SOURCE_WEIGHTS[src] * hit for src, hit in signals.items())
        + agreement_bonus
        - contradiction_penalty
    )
    score = round(max(0.0, min(1.0, raw_score)), 4)

    breakdown = {
        **{key: signals[src] for src, key in SOURCE_SIGNAL_KEYS.items()},
        "sources_supporting": sources_supporting,
        "agreement_bonus": agreement_bonus,
        "contradiction_penalty": contradiction_penalty,
        "weights": {
            "prometheus": PROM_WEIGHT,
            "loki": LOKI_WEIGHT,
            "docker": DOCKER_WEIGHT,
            "health": HEALTH_WEIGHT,
            "deploy": DEPLOYMENTS_WEIGHT,
        },
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
