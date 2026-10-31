"""Read the fixture incidents (see fixtures/README.md) the way db.py reads the database.

Used by the tests and by dev_server.py, so the dashboard can be built and looked at
without the live stack. Not imported by main.py.
"""

import json
from pathlib import Path

FIXTURE_DIR = Path(__file__).parent / "fixtures"

# Fixed so fixture timestamps read the same on every run: shortly after the last event
# of the awaiting_approval fixture, before its request expires.
FIXTURE_NOW = "2026-10-30T16:00:00+00:00"


def load() -> dict[int, dict]:
    """Every fixture incident's rows, keyed by incident id."""
    incidents = {}
    for path in sorted(FIXTURE_DIR.glob("*.json")):
        rows = json.loads(path.read_text(encoding="utf-8"))
        rows.pop("_provenance", None)
        incidents[rows["incident"]["id"]] = rows
    return incidents


def overview_rows(incidents: dict[int, dict]) -> list[dict]:
    """The rows db.overview() would return for these incidents, newest first."""
    rows = []
    for data in incidents.values():
        incident = data["incident"]
        memory = data.get("memory")
        audit = data["audit"]
        rows.append({
            **incident,
            "investigation_status": memory["status"] if memory else None,
            "root_cause_category": memory["root_cause_category"] if memory else None,
            "tier_used": (memory["record"] or {}).get("tier_used") if memory else None,
            "llm_unavailable": any(row["event_type"] == "llm_unavailable" for row in audit),
            "investigation_failed": any(row["event_type"] == "investigation_failed" for row in audit),
            "pending_approvals": sum(1 for a in data["approvals"] if a["status"] == "pending"),
            "audit_events": len(audit),
        })
    return sorted(rows, key=lambda row: row["first_seen_at"], reverse=True)


def long_incident(passes: int = 60) -> dict:
    """A synthetic investigation that ran far longer than any fixture, for stress tests.

    Not a fixture file: it exists to check that a long trace stays fast, bounded and
    readable. Every pass reads two sources, scores two hypotheses and routes back for more.
    """
    base = {"id": 900, "service_name": "checkout-service", "alertname": "HighErrorRate", "severity": "critical", "status": "active",
            "first_seen_at": "2026-10-30T10:00:00+00:00", "last_seen_at": "2026-10-30T12:00:00+00:00", "alert_count": 3}
    evidence, hypotheses, audit = [], [], []
    for n in range(1, passes + 1):
        for source in ("query_prometheus", "query_loki"):
            evidence.append({"id": len(evidence) + 1, "source": source, "iteration": n, "collected_at": "2026-10-30T10:30:00+00:00",
                             "summary": f"{source} pass {n}", "raw_data": {"status": "ok", "lines": ["x" * 120] * 400}})
        for category, score in (("deploy", 0.4), ("config", 0.1)):
            hypotheses.append({"id": len(hypotheses) + 1, "iteration": n, "created_at": "2026-10-30T10:30:00+00:00",
                               "description": f"{category} hypothesis on pass {n}", "score": score, "score_breakdown": {"sources_supporting": 1}})
        audit.append({"id": len(audit) + 1, "created_at": "2026-10-30T10:30:00+00:00", "node": "observer", "event_type": "observer_pass",
                      "detail": {"iteration": n, "dispatched_tools": ["query_prometheus", "query_loki"]}, "reasoning_text": None})
        audit.append({"id": len(audit) + 1, "created_at": "2026-10-30T10:31:00+00:00", "node": "diagnoser", "event_type": "diagnosis",
                      "detail": {"iteration": n, "confidence": 0.4, "confidence_threshold": 0.75,
                                 "hypotheses": [{"description": f"deploy hypothesis on pass {n}", "category": "deploy", "score": 0.4}]},
                      "reasoning_text": f"deploy scores 0.40: deploy hypothesis on pass {n}"})
        audit.append({"id": len(audit) + 1, "created_at": "2026-10-30T10:32:00+00:00", "node": "router", "event_type": "continuing",
                      "detail": {"iteration": n, "confidence": 0.4}, "reasoning_text": "confidence too low (0.40)"})
    return {"incident": base, "evidence": evidence, "hypotheses": hypotheses, "audit": audit, "approvals": [], "memory": None}
