"""Offline replay: re-score stored incidents with the current evidence-validity logic.

A run stores what it read (evidence rows) and what it proposed (hypotheses). This
asks what today's code would conclude from the same evidence, without running an
agent: it is a check on the deterministic part of the system (marker history,
onset anchoring, scoring, the pre-action symptom check) and says nothing about how
a model behaves.

The stored evidence is not trusted blindly. A deployment read is re-annotated
against the marker history as it stood when the evidence was collected, a read of
the latency histogram is given the measure the current code would attach, and a
measure the run stored is replaced. Everything read from the world arrives through
injected functions, so the core is a pure function of stored rows.

Usage: python -m phoenix.graph.replay [--labels FILE] [--incident ID ...] [--json OUT] [--offline]
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from datetime import datetime

from phoenix.graph import correlation, investigation, scoring, symptom
from phoenix.graph.schemas import Hypothesis
from phoenix.graph.state import AgentState

THRESHOLD = AgentState.model_fields["confidence_threshold"].default
THIN_MARGIN = 0.05  # closer than this to the bar is a pass worth a second look

_ANNOTATIONS = ("correlation", "delta_seconds", "in_window", "superseded_by", "incident_started_at")


def _moment(value) -> datetime | None:
    return correlation.parse_timestamp(value)


def _epoch(value) -> float | None:
    moment = _moment(value)
    return moment.timestamp() if moment else None


def _bare(marker: dict) -> dict:
    return {key: value for key, value in marker.items() if key not in _ANNOTATIONS}


_LIMIT = re.compile(r"'limit':\s*(\d+)")
DEFAULT_MARKER_LIMIT = 10  # the deployment tool's own default


def _reread_deployments(raw: dict, summary: str, history: list[dict], collected_at, onset) -> tuple[dict, list[dict]]:
    """The deployment read as today's code would have annotated it, from the history the run could see.

    How many markers the run asked for is in the tool call it stored, not in how many
    came back: a short history returns a short list, and cutting the replay to that
    length would drop the very markers a later one superseded.
    """
    found = _LIMIT.search(summary or "")
    limit = int(found.group(1)) if found else DEFAULT_MARKER_LIMIT
    seen_until = _moment(collected_at)
    visible = [
        _bare(marker) for marker in history
        if seen_until is None or ((_moment(marker.get("timestamp")) or seen_until) <= seen_until)
    ]
    annotated = correlation.correlate_all(visible[:limit], onset)
    rejected = [
        {"source": "get_recent_deployments",
         "what": f"{m.get('image_tag')} @ {m.get('timestamp')}", "why": m["correlation"]}
        for m in annotated if m.get("correlation") in scoring._RULED_OUT_BY_TIME
    ]
    return {**raw, "deployments": annotated}, rejected


def rescore_incident(
    incident: dict,
    evidence_rows: list[dict],
    hypothesis_rows: list[dict],
    history: list[dict],
    *,
    latency_fn,
    symptom_fn,
    threshold: float = THRESHOLD,
) -> dict:
    """What the current code concludes from one stored incident.

    `latency_fn(service, at, onset)` returns the latency measure as of a past moment;
    `symptom_fn(category, service, at)` returns the symptom state as of one. `history`
    is the service's deployment markers, newest first.
    """
    from phoenix.tools import latency_tool  # imported here: the tool module reads the live incident clock

    onset = _moment(incident["first_seen_at"])
    service = incident["service_name"]
    items: list[dict] = []
    rejections: list[dict] = []

    for row in evidence_rows:
        raw = copy.deepcopy(row["raw_data"])
        if row["source"] == "get_recent_deployments" and isinstance(raw, dict) and isinstance(raw.get("deployments"), list):
            raw, rejected = _reread_deployments(raw, row["summary"], history, row.get("collected_at"), onset)
            rejections.extend(rejected)
        elif row["source"] == "query_prometheus" and isinstance(raw, dict):
            job = latency_tool.latency_read_job(row["summary"])
            if job is not None:
                measure = latency_fn(job, _epoch(row.get("collected_at")), onset)
                raw["latency_measure"] = measure
                if isinstance(measure, dict) and measure.get("verdict") == "elevated_before_onset_only":
                    rejections.append({"source": "query_prometheus", "what": f"latency of {job}", "why": "elevated_before_onset_only"})
        items.append({
            "iteration": row.get("iteration", 1), "source": row["source"],
            "collected_at": str(row.get("collected_at")), "summary": row["summary"], "raw_data": raw,
        })

    proposed: dict[str, str] = {}
    for row in hypothesis_rows:
        category = (row.get("score_breakdown") or {}).get("category")
        if category and category not in proposed:
            proposed[category] = row["description"]
    scored = scoring.score_all(items, [Hypothesis(description=d, category=c) for c, d in proposed.items()])

    hypotheses = [
        {"category": h.category, "score": score, "breakdown": breakdown,
         "supported_by": investigation._supporting_sources(breakdown)}
        for h, score, breakdown in scored
    ]
    predicted = hypotheses[0] if hypotheses else None

    observed = None
    blocked = False
    if predicted and predicted["category"] in symptom.PROBES:
        decided_at = max((_epoch(r.get("created_at")) or 0 for r in hypothesis_rows), default=0) or None
        observed = symptom_fn(predicted["category"], service, decided_at)
        blocked = symptom.blocks_action(observed)

    score = predicted["score"] if predicted else 0.0
    return {
        "incident_id": incident["id"], "service": service, "alertname": incident.get("alertname"),
        "onset": onset.isoformat() if onset else None,
        "predicted_category": predicted["category"] if predicted else None,
        "score": score, "threshold": threshold, "hypotheses": hypotheses,
        "supported_by": predicted["supported_by"] if predicted else [],
        "rejections": rejections,
        "symptom": observed, "blocked_by_symptom": blocked,
        "would_remediate": score >= threshold and not blocked,
    }


def judge(result: dict, label: dict | None, threshold: float = THRESHOLD) -> dict:
    """Compare a replayed result with what the incident really was.

    The margin is reported, not just pass or fail: a false positive at 0.74 and a true
    positive at 0.76 both pass and both sit one nudge from the other answer.
    """
    if not label:
        return {"passed": None, "margin": None, "suspicious": False, "protected_by": None}
    score = result["score"]
    protected_by = None
    if label["expect"] == "ambiguous":
        passed = not result["would_remediate"]
        margin = round(threshold - score, 3)
        if passed and result["blocked_by_symptom"] and score >= threshold:
            protected_by = "symptom_check"
    else:
        passed = result["predicted_category"] == label["expect"] and result["would_remediate"]
        margin = round(score - threshold, 3)
    return {
        "passed": passed, "margin": margin, "protected_by": protected_by,
        "suspicious": abs(margin) < THIN_MARGIN or protected_by is not None,
        "strict": label.get("strict", True),
    }


# ---- loading ------------------------------------------------------------------------------


def _load(incident_ids: list[int] | None):
    from phoenix.graph import persist

    pool = persist._get_pool()
    if pool is None:
        sys.exit("replay: DATABASE_URL is not set or the database is unreachable")
    with pool.connection() as conn:
        incidents = conn.execute(
            "SELECT id, service_name, alertname, first_seen_at FROM incidents ORDER BY id"
        ).fetchall()
        evidence = conn.execute(
            "SELECT incident_id, source, summary, raw_data, iteration, collected_at FROM evidence ORDER BY id"
        ).fetchall()
        hypotheses = conn.execute(
            "SELECT incident_id, description, score, score_breakdown, created_at FROM hypotheses ORDER BY id"
        ).fetchall()
    wanted = set(incident_ids) if incident_ids else None
    rows = {}
    for iid, service, alertname, first_seen in incidents:
        if wanted is None or iid in wanted:
            rows[iid] = ({"id": iid, "service_name": service, "alertname": alertname, "first_seen_at": first_seen}, [], [])
    for iid, source, summary, raw, iteration, collected in evidence:
        if iid in rows:
            rows[iid][1].append({"source": source, "summary": summary, "raw_data": raw, "iteration": iteration, "collected_at": collected})
    for iid, description, score, breakdown, created in hypotheses:
        if iid in rows:
            rows[iid][2].append({"description": description, "score": float(score), "score_breakdown": breakdown, "created_at": created})
    return rows


def main(argv: list[str] | None = None) -> int:
    from chaos.lib import deploy_tracker
    from phoenix.tools import latency_tool

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--labels", help="JSON file: {incident id: {expect, note?, strict?}}")
    parser.add_argument("--incident", type=int, nargs="*", help="only these incident ids")
    parser.add_argument("--json", dest="json_out", help="write the full results here")
    parser.add_argument("--offline", action="store_true", help="do not read Prometheus (latency and symptom unknown)")
    args = parser.parse_args(argv)

    labels = {int(k): v for k, v in json.load(open(args.labels, encoding="utf-8")).items()} if args.labels else {}
    rows = _load(args.incident)

    if args.offline:
        latency_fn = lambda service, at, onset: dict(latency_tool.UNAVAILABLE)  # noqa: E731
        symptom_fn = lambda category, service, at: {"state": "unknown"}  # noqa: E731
    else:
        latency_fn = lambda service, at, onset: latency_tool.get_latency_measure(service, at=at, onset=onset)  # noqa: E731
        symptom_fn = lambda category, service, at: symptom.current_symptom(category, service, at=at)  # noqa: E731

    histories: dict[str, list[dict]] = {}
    results = []
    for iid in sorted(rows):
        incident, evidence, hypotheses = rows[iid]
        history = histories.setdefault(incident["service_name"], deploy_tracker.get_recent_deployments(incident["service_name"], 1000))
        result = rescore_incident(incident, evidence, hypotheses, history, latency_fn=latency_fn, symptom_fn=symptom_fn)
        result["label"] = labels.get(iid)
        result["judgement"] = judge(result, result["label"])
        results.append(result)

    _print(results)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=1, default=str)
    judged = [r for r in results if r["judgement"]["passed"] is not None and r["judgement"].get("strict", True)]
    return 0 if all(r["judgement"]["passed"] for r in judged) else 1


def _print(results: list[dict]) -> None:
    print(f"{'inc':>4} {'service':<16} {'expect':<13} {'predicted':<12} {'score':>5} {'act':>3} {'sym':>7} "
          f"{'verdict':<7} {'margin':>7}  notes")
    for r in results:
        j = r["judgement"]
        verdict = "-" if j["passed"] is None else ("PASS" if j["passed"] else "FAIL")
        expect = (r["label"] or {}).get("expect", "-")
        notes = []
        if r["rejections"]:
            notes.append("rejected: " + "; ".join(sorted({x["why"] for x in r["rejections"]})))
        if j["protected_by"]:
            notes.append("protected by symptom check")
        if j["suspicious"]:
            notes.append("THIN MARGIN")
        symptom_state = (r["symptom"] or {}).get("state", "-")
        margin = "-" if j["margin"] is None else f"{j['margin']:+.3f}"
        print(f"{r['incident_id']:>4} {r['service']:<16} {expect:<13} {str(r['predicted_category']):<12} "
              f"{r['score']:>5.2f} {'yes' if r['would_remediate'] else 'no':>3} {symptom_state:>7} "
              f"{verdict:<7} {margin:>7}  {' | '.join(notes)}")
    judged = [r for r in results if r["judgement"]["passed"] is not None]
    passed = sum(1 for r in judged if r["judgement"]["passed"])
    print(f"\n{len(results)} incidents, {len(judged)} labelled, {passed} passed, {len(judged) - passed} failed, "
          f"{sum(1 for r in results if r['judgement']['suspicious'])} with a thin margin or symptom-only protection")


if __name__ == "__main__":
    sys.exit(main())
