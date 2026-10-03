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
    # Specific enough to separate a query regression from generic "overload":
    # a request duration climbing on one endpoint, not system-wide CPU/memory
    # pressure. Keywords match what query_prometheus/query_loki evidence
    # actually carries (request duration metric names, slow-request log lines)
    # rather than code identifiers, which scoring must never see -- the LLM
    # names the category, the evidence has to independently support it.
    "slow_query": [
        "request_duration", "queryseconds", "query_seconds", "db_query", "slowquery",
        "slow query", "slow", "p99", "full table", "fullscan", "sequential scan", "seq scan",
        # Postgres's own slow-statement log line ("duration: 1023.4 ms  statement: ...")
        # as shipped to Loki by log_min_duration_statement.
        "duration:", "statement:",
    ],
    # Distinct from "overload" for the same reason: a monotonically growing
    # working set over many samples is a different signal than a single
    # high-CPU/high-latency reading, and conflating them would let a one-off
    # spike satisfy a leak hypothesis.
    "memory_leak": [
        "memory leak", "leak", "leaking", "growing", "unbounded", "resident memory",
        "resident_memory", "working set", "working_set", "rss", "monotonic", "evict",
    ],
    "unknown": [],
}


def _is_failure(payload) -> bool:
    """True of the error envelope the five read-only tools write when a read fails.

    Four of them write it flat -- phoenix/tools/prometheus_tool.py, loki_tool.py,
    docker_tool.py and deploy_tool.py all return
    ``{"status": "error", "error": <message>}`` -- and inspect_health nests one
    under container and one under app, so the same envelope arrives a level down
    in the one tool that asks two systems a question.

    Both parts of the envelope are required. A status field alone is not a
    failure: a successful Prometheus response says "status": "success" at the
    top and a metric inside its data can be labelled status="error" all day, and
    a payload that only looked like a failure would be discarded as if the read
    had never happened.
    """
    return isinstance(payload, dict) and payload.get("status") == "error" and "error" in payload


def _is_usable(evidence_item: dict) -> bool:
    """Usable = the read returned something, rather than failing.

    A failure is not a weaker measurement, it is no measurement. What it carries
    is the transport's own complaint -- "Connection refused", "Read timed out" --
    and both are network keywords below, so scoring a failure would let a broken
    read satisfy the network category, count toward sources_supporting, and
    suppress the contradiction penalty for a hypothesis that nothing observed.
    A failed measurement making a guess look better-supported than no measurement
    at all is the one direction this module must never be wrong in.

    So a payload is dropped when it is itself the error envelope, and when every
    sub-read nested one level inside it is: that is inspect_health having reached
    neither the container nor the app, which is a read that reached nothing. A
    read that answered on any one channel is kept -- inspect_health that reached
    the container but not /health still measured the container, and throwing that
    away over a missing health probe would cost the crash signal this whole
    module exists to find.
    """
    raw = evidence_item.get("raw_data", {})
    if not isinstance(raw, dict):
        return True
    if _is_failure(raw):
        return False
    sub_reads = [value for value in raw.values() if isinstance(value, dict)]
    return not (sub_reads and all(_is_failure(sub_read) for sub_read in sub_reads))


def _content_values(payload) -> list[str]:
    """Scalar leaf values of a payload, field names discarded."""
    if isinstance(payload, dict):
        return [text for value in payload.values() for text in _content_values(value)]
    if isinstance(payload, (list, tuple)):
        return [text for value in payload for text in _content_values(value)]
    return [str(payload)]


def _runtime_view(payload):
    """A payload with each docker-inspect object reduced to its runtime State.

    `docker inspect` returns the container's static configuration (HostConfig,
    Config, Mounts, ...) next to the one subtree that says what the container
    is doing, State. The static part is full of text that was never evidence:
    HostConfig.MaskedPaths alone lists /proc/latency_stats and
    /proc/timer_stats for every container ever started, and those satisfied
    the latency and slow keywords for a service that was perfectly healthy.
    Scoring State only keeps the crash/restart signal this tool exists to
    provide and drops the configuration it never described.

    Recognised by the pair State + HostConfig, which docker inspect always
    returns together; any other payload is returned unchanged, so test
    fixtures and the other tools' shapes are not affected.
    """
    if isinstance(payload, dict):
        if "State" in payload and "HostConfig" in payload:
            return payload["State"]
        return {key: _runtime_view(value) for key, value in payload.items()}
    if isinstance(payload, (list, tuple)):
        return [_runtime_view(value) for value in payload]
    return payload


def _blob(evidence_item: dict) -> str:
    """Lowercased content of one evidence item for keyword matching:
    raw_data's leaf values only.

    ``summary`` is excluded entirely -- it is always ``f"{tool_name}({call
    arguments})"`` (see nodes.py), so even with the tool-name label stripped
    it still carries the LLM's own query arguments (e.g. the PromQL it chose
    to write), never the data that came back. Letting it in would score the
    LLM's own wording of its request as evidence, regardless of what was
    actually observed. A label -- or the LLM's choice of what to ask for --
    must never satisfy a category keyword on its own; only returned data can.
    """
    try:
        values = _content_values(_runtime_view(evidence_item.get("raw_data", {})))
    except RecursionError:
        values = []
    return "\n".join(values).lower()


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
