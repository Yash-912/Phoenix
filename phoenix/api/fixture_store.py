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
