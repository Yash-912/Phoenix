"""Incident memory: one compact record per finished investigation, and a note
when a new one resembles a finished one. PRD section 11, Phase 8.

This is deliberately small. No embeddings, no retrieval ranking, no graph. A
record is written once an incident reaches a terminal status, and a new
incident is compared to prior records by exactly two keys: the same service and
the same root-cause category. A hit is written to the audit trail as a
"this matches incident #X" note, and that is all it does.

The note is supporting context only. It is written after the run has finished,
so nothing the investigation did could have read it, and nothing here returns a
value the graph routes or scores on. If this module were deleted, every
investigation would decide exactly what it decides now.

Writes go through persist.py's own pool and _execute, so they follow the same
rules as every other trail row: the agent role's grants, the same diagnostic
line on failure, and no exception out of the run. A missing database costs the
record, not the investigation.
"""

from __future__ import annotations

from phoenix.graph import persist
from phoenix.graph.remediation_policy import TIER_2_ACTIONS
from phoenix.graph.state import AgentState

# Statuses that mean the run has stopped. "confident" and "investigating" are
# transit states; "tier3_investigating" hands the run to another subgraph.
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {
        "resolved",
        "action_unavailable",
        "escalated",
        "insufficient_evidence",
        "pr_opened",
        "pr_exists",
    }
)

SYMPTOM_LIMIT = 5
SYMPTOM_CHARS = 160

INSERT_RECORD = """
INSERT INTO incident_memory (incident_id, service_name, root_cause_category, status, record)
VALUES (%(incident_id)s, %(service_name)s, %(root_cause_category)s, %(status)s, %(record)s)
ON CONFLICT (incident_id) DO NOTHING
"""

SELECT_PRIOR = """
SELECT incident_id, status, record
FROM incident_memory
WHERE service_name = %(service_name)s
  AND root_cause_category = %(root_cause_category)s
  AND incident_id <> %(incident_id)s
ORDER BY incident_id DESC
LIMIT 1
"""


def _root_cause(state: AgentState) -> dict | None:
    """The leading hypothesis, as the scorer ranked it. None if nothing was scored."""
    if not state.hypotheses:
        return None
    leader = max(state.hypotheses, key=lambda scored: scored.score)
    return {
        "category": leader.hypothesis.category,
        "description": leader.hypothesis.description,
        "score": leader.score,
    }


def _symptoms(state: AgentState) -> list[str]:
    """Distinct evidence summaries in the order they were gathered, compacted."""
    seen: list[str] = []
    for item in state.evidence:
        summary = item.get("summary") if isinstance(item, dict) else None
        if not summary:
            continue
        text = str(summary)[:SYMPTOM_CHARS]
        if text not in seen:
            seen.append(text)
        if len(seen) >= SYMPTOM_LIMIT:
            break
    return seen


def _tier_used(state: AgentState) -> int | None:
    """Which tier actually acted, read from what happened rather than from intent.

    Tier 3 is the code path, so any Tier 3 status means it ran. Otherwise a
    planned_action means a Tier 1 or Tier 2 action was dispatched, and its
    action name says which. A run that never acted has no tier.
    """
    if state.tier3_status != "not_started":
        return 3
    if state.planned_action:
        return 2 if state.planned_action.get("action") in TIER_2_ACTIONS else 1
    return None


def _fix(state: AgentState) -> dict | None:
    """The reference a later reader needs to find the fix: a PR, or the action taken."""
    if state.pr_result and state.pr_result.get("url"):
        return {"pr_url": state.pr_result["url"], "pr_status": state.pr_result.get("status")}
    if state.planned_action:
        planned = state.planned_action
        return {
            "action": planned.get("action"),
            "container": planned.get("container"),
            "rollback_target": planned.get("expected_version"),
        }
    return None


def _verification(state: AgentState) -> dict | None:
    result = state.verification_result
    if not isinstance(result, dict):
        return None
    return {"outcome": result.get("outcome"), "check": result.get("check")}


def build_record(state: AgentState) -> dict:
    """The compact record for one finished run. Pure: no database, no clock."""
    root = _root_cause(state)
    return {
        "incident_id": state.incident_id,
        "service": state.service_name,
        "status": state.status,
        "symptoms": _symptoms(state),
        "root_cause": root,
        "tier_used": _tier_used(state),
        "fix": _fix(state),
        "verification": _verification(state),
        "escalation_reason": state.escalation_reason,
    }


def _describe(record: dict) -> str:
    root = record.get("root_cause") or {}
    category = root.get("category") or "unknown"
    tier = record.get("tier_used")
    tier_text = f"tier {tier}" if tier else "no action"
    return f"{record['status']}, {tier_text}, root cause {category}"


def find_prior(service_name: str, category: str, incident_id: int) -> dict | None:
    """The most recent other incident with the same service and root-cause category.

    None when nothing matches, or when there is no database to ask. Never raises.
    """
    pool = persist._get_pool()
    if pool is None:
        return None
    try:
        with pool.connection() as conn:
            row = conn.execute(
                SELECT_PRIOR,
                {
                    "service_name": service_name,
                    "root_cause_category": category,
                    "incident_id": incident_id,
                },
            ).fetchone()
    except Exception as exc:  # noqa: BLE001 - an unreadable memory is no match, not a failed run
        persist._diagnose(
            persist._failure_key("incident_memory", exc),
            f"could not read prior incidents for {service_name}/{category}: {exc}",
        )
        return None
    if not row:
        return None
    prior_id, prior_status, prior_record = row[0], row[1], row[2]
    if isinstance(prior_record, dict):
        described = _describe(prior_record)
    else:
        described = f"status {prior_status}"
    return {"incident_id": prior_id, "status": prior_status, "summary": described}


def _note_match(state: AgentState, category: str, prior: dict) -> None:
    """Write the supporting-context note to the trail. Never changes the run."""
    text = (
        f"this matches incident #{prior['incident_id']}: same service ({state.service_name}) "
        f"and root-cause category ({category}); that incident ended {prior['summary']}. "
        "Recorded as context only; it did not influence this run's investigation, scoring or action."
    )
    persist.record_audit(
        state.incident_id,
        "memory",
        "prior_incident_match",
        {
            "matches_incident": prior["incident_id"],
            "prior_status": prior["status"],
            "service": state.service_name,
            "root_cause_category": category,
            "supporting_context_only": True,
        },
        text,
    )
    print(f"[memory] {text}")


def _write_record(record: dict) -> None:
    persist._execute(
        INSERT_RECORD,
        {
            "incident_id": record["incident_id"],
            "service_name": record["service"],
            "root_cause_category": (record["root_cause"] or {}).get("category"),
            "status": record["status"],
            "record": persist._jsonb(record),
        },
    )


def remember_run(result) -> None:
    """Called once after a graph run returns. Matches, notes, and records.

    result is the state langgraph returned: an AgentState, or the dict it
    returns. A result that is not an investigation state (a test double, an
    error envelope) is skipped with one line, not raised.

    The note is written before the record, so the match cannot find the
    incident's own row even though the id exclusion would also prevent it.

    Never raises.
    """
    try:
        state = result if isinstance(result, AgentState) else AgentState.model_validate(result)
    except Exception as exc:  # noqa: BLE001 - a result we cannot read is a skipped record
        print(f"[memory] no record: the run result is not an investigation state ({type(exc).__name__})")
        return

    if state.status not in TERMINAL_STATUSES:
        return

    try:
        record = build_record(state)
        root = record["root_cause"]
        if root and root.get("category"):
            prior = find_prior(state.service_name, root["category"], state.incident_id)
            if prior is not None:
                _note_match(state, root["category"], prior)
        _write_record(record)
    except Exception as exc:  # noqa: BLE001 - the record is a by-product of the diagnosis
        persist._diagnose(
            persist._failure_key("incident_memory", exc),
            f"incident record not written for incident {state.incident_id}: {exc}",
        )
