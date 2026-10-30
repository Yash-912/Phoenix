"""Turn the rows Phoenix wrote for one incident into the trace the dashboard shows.

Pure: rows in, a dict out, no database and no clock (the caller passes `now`). The
point of the dashboard is that someone can narrate an incident from this trace alone,
so the shape follows the questions they would ask, in order: what happened, why did
the agent think so, what did it do, did it work, and what is left for a human.

Decisions come first. The raw audit rows, and the LLM prompts and responses once the
graph records them, sit one call deeper (see audit_rows). Everything that leaves this
module has been through redact.Redactor, and the redaction counts travel with it.

The graph's own vocabulary (event names, statuses, tiers) is repeated here rather
than imported: the API image does not carry the graph code. A name this module does
not know still appears in the timeline, titled from the event name, so a new graph
event is shown late rather than dropped.
"""

import json
from datetime import datetime, timezone

from redact import Redactor

# --- vocabulary -------------------------------------------------------------

# How an investigation ended, as the list and the detail page name it. tone drives the
# colour of the chip, never the meaning: the label and summary say it in words too.
OUTCOMES: dict[str, dict[str, str]] = {
    "resolved": {
        "label": "Resolved", "tone": "good",
        "summary": "Phoenix acted and verified that the symptom went away.",
    },
    "pr_opened": {
        "label": "PR opened", "tone": "good",
        "summary": "Phoenix found the code defect and opened a pull request. It never merges; a person reviews it.",
    },
    "pr_exists": {
        "label": "PR already open", "tone": "good",
        "summary": "Phoenix reached the same fix an open pull request already proposes, so it opened nothing new.",
    },
    "insufficient_evidence": {
        "label": "Insufficient evidence", "tone": "neutral",
        "summary": "Phoenix looked, found no hypothesis two independent sources support, and took no action.",
    },
    "escalated": {
        "label": "Escalated", "tone": "warn",
        "summary": "Phoenix stopped and handed the incident to a person, with the reason recorded.",
    },
    "action_unavailable": {
        "label": "No action available", "tone": "warn",
        "summary": "Phoenix identified a cause that has no allowed action, so it did nothing and stopped.",
    },
    "llm_unavailable": {
        "label": "Model unavailable", "tone": "bad",
        "summary": "The language model could not be reached. Nothing was weighed, so no finding was made.",
    },
    "investigation_failed": {
        "label": "Investigation failed", "tone": "bad",
        "summary": "The investigation crashed or kept failing and was given up on.",
    },
    "awaiting_approval": {
        "label": "Awaiting approval", "tone": "attention",
        "summary": "Phoenix chose an action and is paused until an approver answers.",
    },
    "investigating": {
        "label": "Investigating", "tone": "neutral",
        "summary": "Phoenix has started and has not reached an outcome yet.",
    },
    "not_started": {
        "label": "Not started", "tone": "neutral",
        "summary": "The alert is recorded but Phoenix has not picked it up yet.",
    },
}

TIER_2_ACTIONS = frozenset({"rollback_deployment", "rollback_config"})

TIERS: dict[int, dict[str, str]] = {
    1: {"label": "Tier 1", "meaning": "a reversible runtime action, such as a restart"},
    2: {"label": "Tier 2", "meaning": "a rollback of a deploy or a config change"},
    3: {"label": "Tier 3", "meaning": "a code fix proposed as a pull request, never merged by Phoenix"},
}

# event_type -> (kind, title). kind groups steps for the reader; "stop" marks the step
# that ended the run. minor steps are shown collapsed by default.
EVENTS: dict[str, tuple[str, str]] = {
    "observer_pass": ("observe", "Read evidence"),
    "diagnosis": ("diagnose", "Scored the hypotheses"),
    "continuing": ("route", "Not confident yet, looked again"),
    "threshold_reached": ("route", "Confident enough to act"),
    "insufficient_evidence": ("stop", "Stopped: insufficient evidence"),
    "escalated": ("stop", "Escalated to a person"),
    "symptom_checked": ("check", "Checked the symptom is still present"),
    "symptom_cleared": ("stop", "Symptom already gone, did not act"),
    "approval_requested": ("approval", "Asked for approval"),
    "approval_granted": ("approval", "Approval granted"),
    "approval_expired": ("approval", "Approval request expired"),
    "approval_not_granted": ("stop", "Approval not granted"),
    "approval_unavailable": ("stop", "Approval could not be asked for"),
    "approval_mismatch": ("stop", "Approval did not match the action"),
    "action_blocked_by_policy": ("stop", "Blocked by policy"),
    "action_attempts_exhausted": ("stop", "Out of action attempts"),
    "action_redundant": ("stop", "Action would change nothing"),
    "action_failed": ("stop", "Action failed"),
    "action_unavailable": ("stop", "No allowed action for this cause"),
    "action_started": ("act", "Started the action"),
    "action_finished": ("act", "Action finished"),
    "action_executed": ("act", "Why this action"),
    "verification": ("verify", "Checked whether it worked"),
    "verification_failed": ("stop", "Could not confirm recovery"),
    "tier3_handoff": ("tier3", "Handed off to code investigation"),
    "investigation_context": ("tier3", "Located the service's code"),
    "investigation_pass": ("tier3", "Read code"),
    "investigation_concluded": ("tier3", "Concluded the code investigation"),
    "no_defect_found": ("stop", "No code defect pinned down"),
    "patch_target": ("tier3", "Chose where to patch"),
    "patch_proposed": ("tier3", "Proposed a patch"),
    "patch_rejected": ("stop", "Patch rejected"),
    "patch_generation_failed": ("stop", "Could not generate a patch"),
    "validation_result": ("tier3", "Ran tests and lint on the patch"),
    "validation_failed": ("stop", "Patch failed validation"),
    "branch_pushed": ("tier3", "Pushed the fix branch"),
    "pr_duplicate_check_failed": ("tier3", "Could not check for an existing PR"),
    "pr_duplicate": ("tier3", "An open PR already has this fix"),
    "pr_result": ("tier3", "Opened the pull request"),
    "pr_blocked": ("stop", "Pull request blocked"),
    "pr_failed": ("stop", "Pull request failed"),
    "remote_branch_kept": ("tier3", "Kept the pushed branch"),
    "remote_branch_deleted": ("tier3", "Deleted the unused branch"),
    "remote_branch_delete_failed": ("tier3", "Could not delete the unused branch"),
    "prior_incident_match": ("memory", "Matches an earlier incident"),
    "llm_unavailable": ("failure", "Model could not be reached"),
    "llm_request_rejected": ("failure", "Model rejected the request"),
    "investigation_failed": ("failure", "Investigation crashed and was given up on"),
}

MINOR_EVENTS = frozenset({"observer_pass", "investigation_pass", "investigation_context", "symptom_checked", "action_started"})

# The event types under which a graph that records LLM calls would write them. The graph
# does not today (Audit Level 3 is not implemented), so these are looked for, not assumed.
LLM_EVENTS = frozenset({"llm_call", "llm_prompt", "llm_response"})

# Evidence larger than this is cut before it leaves the API; the cut is marked.
RAW_LIMIT = 4000
FACT_LIMIT = 8
FACT_TEXT_LIMIT = 160


# --- small helpers ----------------------------------------------------------


def iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _parse(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _clip_raw(value):
    """Keep a JSON value whole when it is small, else a marked preview of it."""
    try:
        text = json.dumps(value, default=str)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) <= RAW_LIMIT:
        return value
    return {"truncated": True, "original_chars": len(text), "preview": text[:RAW_LIMIT]}


def _humanize(event_type: str) -> str:
    return event_type.replace("_", " ").capitalize()


def _facts(detail: dict) -> dict:
    """The small, readable values of an audit detail, for the line under a step.

    Scalars and short lists of scalars only. Long text, diffs and nested objects stay
    in the raw audit row, one call deeper.
    """
    facts: dict = {}
    for key, value in detail.items():
        if key in ("iteration", "diff", "hunks"):
            continue
        if isinstance(value, bool) or value is None:
            if value is not None:
                facts[key] = value
        elif isinstance(value, (int, float)):
            facts[key] = value
        elif isinstance(value, str):
            if value and len(value) <= FACT_TEXT_LIMIT:
                facts[key] = value
        elif isinstance(value, list) and value and len(value) <= FACT_LIMIT and all(isinstance(v, (str, int, float)) for v in value):
            facts[key] = value
        if len(facts) >= FACT_LIMIT:
            break
    return facts


# --- outcome ----------------------------------------------------------------


def derive_outcome(
    memory_status: str | None,
    *,
    llm_unavailable: bool = False,
    investigation_failed: bool = False,
    pending_approvals: int = 0,
    audit_events: int = 0,
) -> str:
    """How the investigation stands. The memory row is the graph's own final word and wins.

    Only terminal runs reach incident_memory. A model outage, a failed investigation and
    a run paused for approval do not, so those are read from what the audit trail holds.
    """
    if memory_status:
        return memory_status
    if investigation_failed:
        return "investigation_failed"
    if pending_approvals:
        return "awaiting_approval"
    if llm_unavailable:
        return "llm_unavailable"
    return "investigating" if audit_events else "not_started"


def outcome_entry(key: str) -> dict:
    meta = OUTCOMES.get(key)
    if meta is None:
        # A status the graph added after this was written: shown in its own words.
        meta = {"label": _humanize(key), "tone": "neutral", "summary": "Phoenix recorded this outcome."}
    return {"key": key, **meta}


def overview_row(row: dict) -> dict:
    """One incident-list row, with the outcome worked out and named."""
    key = derive_outcome(
        row.get("investigation_status"),
        llm_unavailable=bool(row.get("llm_unavailable")),
        investigation_failed=bool(row.get("investigation_failed")),
        pending_approvals=int(row.get("pending_approvals") or 0),
        audit_events=int(row.get("audit_events") or 0),
    )
    return {
        "id": row["id"],
        "service_name": row["service_name"],
        "alertname": row["alertname"],
        "severity": row["severity"],
        "alert_status": row["status"],
        "first_seen_at": iso(row.get("first_seen_at")),
        "last_seen_at": iso(row.get("last_seen_at")),
        "alert_count": row.get("alert_count"),
        "outcome": outcome_entry(key),
        "root_cause_category": row.get("root_cause_category"),
        "tier_used": row.get("tier_used"),
        "pending_approvals": int(row.get("pending_approvals") or 0),
    }


# --- sections ---------------------------------------------------------------


def _timeline(audit: list[dict], redactor: Redactor) -> list[dict]:
    steps = []
    for row in audit:
        detail = _dict(row.get("detail"))
        event_type = row["event_type"]
        kind, title = EVENTS.get(event_type, ("other", _humanize(event_type)))
        if event_type in LLM_EVENTS:
            kind, title = "llm", _humanize(event_type)
        ok = None
        if event_type == "action_finished":
            ok = bool(detail.get("ok"))
            if not ok:
                title = "Action failed"
        steps.append({
            "id": row.get("id"),
            "at": iso(row.get("created_at")),
            "node": row["node"],
            "event_type": event_type,
            "kind": kind,
            "title": title,
            "text": redactor.text(row.get("reasoning_text") or ""),
            "iteration": detail.get("iteration"),
            "minor": event_type in MINOR_EVENTS,
            "ok": ok,
            "facts": redactor.value(_facts(detail)),
        })
    return steps


def _hypotheses(hypothesis_rows: list[dict], audit: list[dict], redactor: Redactor) -> dict:
    """The hypotheses by pass, with the last pass's leader and what it ranked above.

    The table has no category column, so a category is joined back from the diagnoser's
    own audit row by description. A discarded hypothesis is one the final pass ranked
    below the leader; the trace says by how much, because "discarded" without a score
    is an assertion and the score is the evidence.
    """
    categories: dict[tuple[int | None, str], str] = {}
    for row in audit:
        if row["event_type"] != "diagnosis":
            continue
        detail = _dict(row.get("detail"))
        for item in detail.get("hypotheses") or []:
            if isinstance(item, dict) and item.get("description"):
                categories[(detail.get("iteration"), item["description"])] = item.get("category")

    by_iteration: dict[int, list[dict]] = {}
    for row in hypothesis_rows:
        score = float(row["score"])
        breakdown = _dict(row.get("score_breakdown"))
        by_iteration.setdefault(row["iteration"], []).append({
            "id": row.get("id"),
            "description": redactor.text(row["description"]),
            "category": categories.get((row["iteration"], row["description"])),
            "score": score,
            "breakdown": redactor.value(breakdown),
        })

    passes = []
    for iteration in sorted(by_iteration):
        items = sorted(by_iteration[iteration], key=lambda item: -item["score"])
        for rank, item in enumerate(items):
            item["rank"] = rank + 1
        passes.append({"iteration": iteration, "items": items})

    final = passes[-1]["items"] if passes else []
    leader = final[0] if final else None
    discarded = [
        {**item, "behind_leader_by": round(leader["score"] - item["score"], 4)}
        for item in final[1:]
    ] if leader else []

    confidence = threshold = None
    for row in audit:
        if row["event_type"] == "diagnosis":
            detail = _dict(row.get("detail"))
            confidence, threshold = detail.get("confidence"), detail.get("confidence_threshold")

    return {
        "passes": passes,
        "leader": leader,
        "discarded": discarded,
        "confidence": confidence,
        "confidence_threshold": threshold,
    }


def _evidence(rows: list[dict], redactor: Redactor) -> list[dict]:
    items = []
    for row in rows:
        raw = row.get("raw_data")
        raw_dict = _dict(raw)
        # The tools write {"status": "error", "error": ...} when a read fails.
        failed = raw_dict.get("status") == "error" or bool(raw_dict.get("error"))
        items.append({
            "id": row.get("id"),
            "source": row["source"],
            "iteration": row["iteration"],
            "collected_at": iso(row.get("collected_at")),
            "summary": redactor.text(row["summary"]),
            "failed": failed,
            "raw_data": redactor.value(_clip_raw(raw)),
        })
    return items


def _approvals(rows: list[dict], now: datetime, redactor: Redactor) -> list[dict]:
    out = []
    for row in rows:
        expires = _parse(row.get("expires_at"))
        pending = row["status"] == "pending"
        out.append({
            "id": row["id"],
            "action": row["action"],
            "container": row["container"],
            "args": redactor.value(row.get("args")),
            "tier": row["tier"],
            "status": row["status"],
            "can_decide": pending and expires is not None and expires > now,
            "overdue": pending and expires is not None and expires <= now,
            "incident_summary": redactor.text(row.get("incident_summary") or ""),
            "plan_reasoning": redactor.text(row.get("plan_reasoning") or ""),
            "evidence": redactor.value(_clip_raw(row.get("evidence"))),
            "requested_at": iso(row.get("requested_at")),
            "expires_at": iso(row.get("expires_at")),
            "decided_at": iso(row.get("decided_at")),
            "decided_by": row.get("decided_by"),
            "decision_channel": row.get("decision_channel"),
        })
    return out


def _actions(audit: list[dict], redactor: Redactor) -> list[dict]:
    """What the agent actually did to the system, one entry per action it started."""
    actions: list[dict] = []
    for row in audit:
        detail = _dict(row.get("detail"))
        if row["event_type"] == "action_started":
            actions.append({
                "action": detail.get("action"),
                "container": detail.get("container"),
                "args": redactor.value(detail.get("args") or []),
                "started_at": iso(row.get("created_at")),
                "finished_at": None,
                "ok": None,
                "error": None,
                "why": None,
            })
        elif row["event_type"] == "action_finished" and actions:
            actions[-1].update(finished_at=iso(row.get("created_at")), ok=bool(detail.get("ok")), error=redactor.text(str(detail.get("error") or "")) or None)
        elif row["event_type"] == "action_executed" and actions:
            actions[-1]["why"] = redactor.text(row.get("reasoning_text") or "")
    return actions


def _tier(audit: list[dict], approvals: list[dict], memory: dict | None, actions: list[dict]) -> dict:
    """Which tier acted and why that one. The "why" is the agent's own recorded reason."""
    tier = None
    record = _dict(_dict(memory).get("record"))
    if isinstance(record.get("tier_used"), int):
        tier = record["tier_used"]
    if tier is None and any(row["event_type"] in ("tier3_handoff", "patch_proposed", "pr_result", "pr_duplicate") for row in audit):
        tier = 3
    if tier is None and actions:
        tier = 2 if actions[0]["action"] in TIER_2_ACTIONS else 1
    policy_mode = None
    for row in audit:
        mode = _dict(row.get("detail")).get("policy_mode")
        if mode:
            policy_mode = mode
    why = next((a["why"] for a in actions if a.get("why")), None)
    if why is None:
        handoff = next((row.get("reasoning_text") for row in audit if row["event_type"] == "tier3_handoff"), None)
        why = handoff
    gate = None
    if approvals:
        gate = "held for approval"
    elif tier in (1, 2):
        gate = "ran without a gate"
    elif tier == 3:
        gate = "stops at a pull request"
    return {
        "tier": tier,
        "label": TIERS[tier]["label"] if tier in TIERS else None,
        "meaning": TIERS[tier]["meaning"] if tier in TIERS else None,
        "policy_mode": policy_mode,
        "gate": gate,
        "why": why,
    }


def _verification(audit: list[dict], redactor: Redactor) -> dict | None:
    last = None
    for row in audit:
        if row["event_type"] == "verification":
            last = row
    if last is None:
        return None
    detail = _dict(last.get("detail"))
    return {
        "outcome": detail.get("outcome"),
        "check": detail.get("check"),
        "action": detail.get("action"),
        "reason": redactor.text(last.get("reasoning_text") or ""),
        "detail": redactor.value(detail.get("detail")),
        "at": iso(last.get("created_at")),
    }


def _tier3(audit: list[dict], redactor: Redactor) -> dict | None:
    """The patch-and-PR path: the defect, each proposal, validation, and what became of the branch."""
    events = [row for row in audit if EVENTS.get(row["event_type"], ("",))[0] == "tier3" or row["event_type"] in ("no_defect_found", "patch_rejected", "patch_generation_failed", "validation_failed", "pr_blocked", "pr_failed")]
    if not events:
        return None
    block: dict = {"defect": None, "target": None, "proposals": [], "validation": None, "branch": None, "branch_cleanup": None, "pr": None, "stopped": None}
    for row in events:
        detail = _dict(row.get("detail"))
        kind = row["event_type"]
        if kind == "investigation_concluded":
            block["defect"] = redactor.value({**{k: detail.get(k) for k in ("defect_found", "file_path", "function_name", "fix_approach")}, "description": row.get("reasoning_text")})
        elif kind == "patch_target":
            block["target"] = redactor.value(detail)
        elif kind == "patch_proposed":
            block["proposals"].append(redactor.value({
                "attempt": detail.get("attempt"), "file_path": detail.get("file_path"), "changed_lines": detail.get("changed_lines"),
                "scope_ok": detail.get("scope_ok"), "diff": detail.get("diff"), "note": row.get("reasoning_text"),
            }))
        elif kind == "validation_result":
            block["validation"] = redactor.value({
                "tests_passed": detail.get("tests_passed"), "lint_passed": detail.get("lint_passed"), "summary": row.get("reasoning_text"),
            })
        elif kind == "branch_pushed":
            block["branch"] = detail.get("branch")
        elif kind in ("remote_branch_kept", "remote_branch_deleted", "remote_branch_delete_failed"):
            block["branch_cleanup"] = {"result": kind.removeprefix("remote_branch_"), "branch": detail.get("branch"), "why": redactor.text(str(detail.get("reason") or detail.get("why") or detail.get("error") or ""))}
        elif kind == "pr_result":
            result = _dict(detail.get("result"))
            block["pr"] = {"status": "opened" if result.get("status") == "ok" else "failed", "url": result.get("url"), "number": result.get("number"), "error": redactor.text(str(result.get("error") or "")) or None}
        elif kind == "pr_duplicate":
            existing = _dict(detail.get("existing_pr"))
            block["pr"] = {"status": "exists", "url": existing.get("url"), "number": existing.get("number"), "error": None}
        elif EVENTS.get(kind, ("",))[0] == "stop":
            block["stopped"] = {"event_type": kind, "reason": redactor.text(row.get("reasoning_text") or "")}
    return block


def _memory(audit: list[dict], memory: dict | None, redactor: Redactor) -> dict:
    matches = []
    for row in audit:
        if row["event_type"] != "prior_incident_match":
            continue
        detail = _dict(row.get("detail"))
        matches.append({
            "incident_id": detail.get("matches_incident"),
            "status": detail.get("prior_status"),
            "category": detail.get("root_cause_category"),
            "text": redactor.text(row.get("reasoning_text") or ""),
        })
    record = _dict(memory).get("record")
    return {"matches": matches, "record": redactor.value(record) if record else None}


def _problems(audit: list[dict], evidence: list[dict], redactor: Redactor) -> list[dict]:
    """Things that went wrong or came back empty, in one place, so they are not hunted for."""
    problems = []
    for item in evidence:
        if item["failed"]:
            problems.append({"kind": "failed_read", "text": f"{item['source']} read failed: {item['summary']}", "at": item["collected_at"]})
    for row in audit:
        kind = EVENTS.get(row["event_type"], ("",))[0]
        if kind in ("failure",):
            problems.append({"kind": row["event_type"], "text": redactor.text(row.get("reasoning_text") or _humanize(row["event_type"])), "at": iso(row.get("created_at"))})
        if row["event_type"] == "action_finished" and not _dict(row.get("detail")).get("ok"):
            problems.append({"kind": "action_failed", "text": redactor.text(row.get("reasoning_text") or "an action failed"), "at": iso(row.get("created_at"))})
    return problems


def _evidence_report(audit: list[dict], redactor: Redactor) -> dict | None:
    for row in audit:
        if row["event_type"] == "insufficient_evidence":
            report = _dict(row.get("detail")).get("evidence_report")
            if report:
                return redactor.value(report)
    return None


def _stop_reason(audit: list[dict], redactor: Redactor) -> str | None:
    reason = None
    for row in audit:
        kind = EVENTS.get(row["event_type"], ("",))[0]
        if kind in ("stop", "failure") and row.get("reasoning_text"):
            reason = redactor.text(row["reasoning_text"])
    return reason


def _narrative(header: dict, hypotheses: dict, actions: list[dict], approvals: list[dict], verification: dict | None, tier3: dict | None, stop_reason: str | None) -> list[str]:
    """The incident in a few plain sentences, built from the sections and nothing else."""
    lines = [f"{header['alertname']} on {header['service_name']} ({header['severity']})."]
    leader = hypotheses["leader"]
    if leader:
        threshold = hypotheses.get("confidence_threshold")
        confidence = hypotheses.get("confidence")
        confidence_text = f" with confidence {confidence:.2f}" if isinstance(confidence, (int, float)) else ""
        if threshold is not None and isinstance(confidence, (int, float)):
            confidence_text += f" against a threshold of {threshold:.2f}"
        category = f"{leader['category']}: " if leader.get("category") else ""
        lines.append(f"Leading hypothesis{confidence_text}: {category}{leader['description']}")
        if hypotheses["discarded"]:
            others = len(hypotheses["discarded"])
            lines.append(f"{others} other {'hypothesis' if others == 1 else 'hypotheses'} ranked below it.")
    for approval in approvals:
        if approval["status"] == "approved":
            lines.append(f"{approval['decided_by']} approved {approval['action']} on {approval['container']} via {approval['decision_channel'] or 'unknown channel'}.")
        elif approval["status"] == "denied":
            lines.append(f"{approval['decided_by']} denied {approval['action']} on {approval['container']}.")
        elif approval["status"] == "expired":
            lines.append(f"No one answered the request for {approval['action']} on {approval['container']}, so it expired and was not approved.")
        elif approval["can_decide"]:
            lines.append(f"Waiting for an approver to answer: {approval['action']} on {approval['container']}.")
    for action in actions:
        if action["ok"] is True:
            lines.append(f"Ran {action['action']} on {action['container']}.")
        elif action["ok"] is False:
            lines.append(f"{action['action']} on {action['container']} failed.")
    if verification:
        verdict = {"pass": "passed", "fail": "failed", "inconclusive": "was inconclusive"}.get(verification["outcome"], str(verification["outcome"]))
        lines.append(f"Verification {verdict}: {verification['reason']}" if verification["reason"] else f"Verification {verdict}.")
    if tier3 and tier3.get("pr"):
        pr = tier3["pr"]
        if pr["status"] == "opened":
            lines.append(f"Opened a pull request for review: {pr['url']}")
        elif pr["status"] == "exists":
            lines.append(f"An open pull request already carries this fix: {pr['url']}")
    if stop_reason and header["outcome"]["key"] in ("escalated", "insufficient_evidence", "action_unavailable", "llm_unavailable", "investigation_failed"):
        lines.append(f"Stopped because: {stop_reason}")
    return lines


# --- entry points -----------------------------------------------------------


def build_trace(rows: dict, now: datetime | None = None) -> dict:
    """Assemble the trace for one incident from its raw rows.

    rows holds: incident (a dict), evidence, hypotheses, audit and approvals (lists of
    dicts, in id order), and memory (a dict or None).
    """
    now = now or datetime.now(timezone.utc)
    redactor = Redactor()
    incident = rows["incident"]
    audit = rows.get("audit") or []
    memory = rows.get("memory")
    approval_rows = rows.get("approvals") or []

    pending = sum(1 for row in approval_rows if row["status"] == "pending" and (_parse(row.get("expires_at")) or now) > now)
    outcome_key = derive_outcome(
        _dict(memory).get("status"),
        llm_unavailable=any(row["event_type"] == "llm_unavailable" for row in audit),
        investigation_failed=any(row["event_type"] == "investigation_failed" for row in audit),
        pending_approvals=pending,
        audit_events=len(audit),
    )
    header = {
        "id": incident["id"],
        "service_name": incident["service_name"],
        "alertname": incident["alertname"],
        "severity": incident["severity"],
        "alert_status": incident["status"],
        "first_seen_at": iso(incident.get("first_seen_at")),
        "last_seen_at": iso(incident.get("last_seen_at")),
        "alert_count": incident.get("alert_count"),
        "outcome": outcome_entry(outcome_key),
    }

    evidence = _evidence(rows.get("evidence") or [], redactor)
    hypotheses = _hypotheses(rows.get("hypotheses") or [], audit, redactor)
    approvals = _approvals(approval_rows, now, redactor)
    actions = _actions(audit, redactor)
    tier = _tier(audit, approvals, memory, actions)
    verification = _verification(audit, redactor)
    tier3 = _tier3(audit, redactor)
    stop_reason = _stop_reason(audit, redactor)

    trace = {
        "incident": header,
        "narrative": _narrative(header, hypotheses, actions, approvals, verification, tier3, stop_reason),
        "stop_reason": stop_reason,
        "evidence_report": _evidence_report(audit, redactor),
        "timeline": _timeline(audit, redactor),
        "hypotheses": hypotheses,
        "evidence": evidence,
        "tier": tier,
        "approvals": approvals,
        "actions": actions,
        "verification": verification,
        "tier3": tier3,
        "memory": _memory(audit, memory, redactor),
        "problems": _problems(audit, evidence, redactor),
        "llm": {
            "recorded": any(row["event_type"] in LLM_EVENTS for row in audit),
            "note": "The graph does not record LLM prompts and responses yet (Audit Level 3 is not implemented)."
            if not any(row["event_type"] in LLM_EVENTS for row in audit) else None,
        },
    }
    trace["redaction"] = redactor.summary()
    return trace


def audit_rows(audit: list[dict]) -> dict:
    """The raw audit rows, redacted, for the one-click-deeper view."""
    redactor = Redactor()
    rows = [{
        "id": row.get("id"),
        "at": iso(row.get("created_at")),
        "node": row["node"],
        "event_type": row["event_type"],
        "kind": "llm" if row["event_type"] in LLM_EVENTS else EVENTS.get(row["event_type"], ("other",))[0],
        "reasoning_text": redactor.text(row.get("reasoning_text") or "") or None,
        "detail": redactor.value(row.get("detail")),
    } for row in audit]
    return {"rows": rows, "redaction": redactor.summary()}


def approval_queue(rows: list[dict], now: datetime | None = None) -> list[dict]:
    """Pending approval requests, newest first, each with the incident it belongs to."""
    now = now or datetime.now(timezone.utc)
    redactor = Redactor()
    queue = []
    for row in rows:
        item = _approvals([row], now, redactor)[0]
        item["incident_id"] = row["incident_id"]
        item["service_name"] = row.get("service_name")
        queue.append(item)
    return queue
