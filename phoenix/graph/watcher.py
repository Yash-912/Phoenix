"""Polls for incidents nobody has investigated yet, and runs the graph on each.

This is the piece that was missing for Phoenix to be autonomous end to end.
phoenix/api creates an incident row when Alertmanager fires; nothing after
that point ever called the graph -- every investigation in this lab so far was
started by a human running `python -m phoenix.graph.graph <id> <service>` by
hand. "Detect" worked; "investigate" needed a person to notice and type a
command. This closes that gap without changing phoenix/api or its container:
it is a second host-side process, run the same way the graph itself is run,
that asks persist.list_unhandled_incidents() what needs starting and starts it.

**Why a poller instead of calling the graph from inside the API request.**
The API container doesn't have the graph's code, its dependencies (langgraph,
openai, the LLM credentials), or network reach to the lab the way this process
does -- the graph runs host-side against localhost-mapped ports, same as every
chaos script, and persist.py's own docstring records that as the deliberate
split between the two services' failure domains. Polling from here needs
nothing added to the API image and crosses no boundary that wasn't already
crossed by running the graph manually.

**Why oldest-unhandled rather than a push from the webhook.** A push would
still need the two processes to agree on a transport (HTTP callback, a queue,
a signal) and to handle the webhook succeeding while the push fails. A poll
against list_unhandled_incidents() needs none of that: the audit trail already
is the record of what has been investigated, so asking "what has no audit row"
on an interval is sufficient and NEVER double-starts a run that already has
one row, even across a watcher restart.

**Why one incident at a time.** Running two investigations concurrently means
sharing nothing that isn't already safe to share (persist's pool is) and
budgeting LLM spend across both at once, which the guardrails were not built
for. A queue of N incidents finishes N times slower this way, which is the
accepted cost of not having to reason about concurrent token budgets yet.

Usage: python -m phoenix.graph.watcher
"""

from __future__ import annotations

import os
import time

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from phoenix.graph import approvals, safety_policy
from phoenix.graph.graph import build_graph
from phoenix.graph.persist import close_persistence, list_unhandled_incidents, max_incident_id, record_audit
from phoenix.graph.state import AgentState

POLL_INTERVAL_SECONDS = int(os.environ.get("PHOENIX_WATCH_INTERVAL_SECONDS", "10"))

# approval id -> (incident id, the compiled app, its thread config). A run that
# asked for an approval is held here, in this process, until the decision lands.
# MemorySaver is in-process, so a restart drops these runs. Their requests then
# expire, and an expiry is an escalation, never an approval.
_paused: dict[int, tuple[int, object, dict]] = {}


def poll_once(after_id: int = 0) -> list[int]:
    """Run every incident newer than `after_id` with no investigation yet.
    Returns the ids it started.

    A single pass is the unit this module is tested against: run_forever is a
    thin loop around it, and nothing here has to fake time passing to be
    exercised.

    One incident's exception does not cancel the rest of the batch, on the
    same reasoning the observer applies to a malformed tool call: whatever is
    wrong with one incident's run is not evidence that the next one would fail
    the same way, and silently never trying it would be a worse answer than
    the run it could not complete.
    """
    started: list[int] = []
    # Read once per pass. A bad configuration raises here, so the watcher stops
    # rather than starting investigations under a policy nobody checked.
    policy_mode = safety_policy.load_config().execution_policy
    for incident_id, service_name in list_unhandled_incidents(after_id):
        print(f"[watcher] incident {incident_id} ({service_name}) has no investigation yet -- starting one")
        started.append(incident_id)
        try:
            app = build_graph(checkpointer=MemorySaver())
            config = {"configurable": {"thread_id": str(incident_id)}}
            result = app.invoke(
                AgentState(incident_id=incident_id, service_name=service_name, policy_mode=policy_mode),
                config,
            )
            _handle_result(incident_id, app, config, result)
        except Exception as exc:
            print(f"[watcher] incident {incident_id} raised {type(exc).__name__}: {exc}")
    return started


def _handle_result(incident_id: int, app, config: dict, result) -> None:
    """Hold a run that paused for an approval, or report one that finished."""
    interrupts = result.get("__interrupt__") if isinstance(result, dict) else None
    if interrupts:
        approval_id = interrupts[0].value["approval_id"]
        _paused[approval_id] = (incident_id, app, config)
        print(f"[watcher] incident {incident_id} paused, waiting on approval {approval_id}")
        return
    status = result.get("status") if isinstance(result, dict) else getattr(result, "status", None)
    print(f"[watcher] incident {incident_id} finished: status={status}")
    if status in ("escalated", "insufficient_evidence"):
        get = result.get if isinstance(result, dict) else lambda key: getattr(result, key, None)
        print(f"[watcher] incident {incident_id} reason={get('escalation_reason')}")
        report = get("evidence_report") or {}
        if report.get("summary"):
            print(f"[watcher] incident {incident_id} {report['summary']}")


def resume_answered() -> list[int]:
    """Expire overdue requests, then resume each paused run whose request is decided.

    A request still pending is left alone. A decided or expired one is resumed, and
    the resumed node reads the decision from the database itself, so this function
    decides nothing. An expired request resumes into an escalation. Returns the
    approval ids resumed.
    """
    try:
        for row in approvals.expire_stale_approvals():
            record_audit(
                row["incident_id"], "watcher", "approval_expired",
                {"approval_id": row["id"], "action": row["action"], "container": row["container"]},
                "no one answered in time, so this is an escalation, not an approval",
            )
    except approvals.ApprovalStoreError as exc:
        print(f"[watcher] could not expire overdue approvals this pass: {exc}")

    resumed: list[int] = []
    for approval_id, (incident_id, app, config) in list(_paused.items()):
        try:
            row = approvals.read_approval(approval_id)
        except approvals.ApprovalStoreError as exc:
            print(f"[watcher] approval {approval_id} could not be read, will retry: {exc}")
            continue
        if row is not None and row["status"] == "pending":
            continue
        del _paused[approval_id]
        resumed.append(approval_id)
        state = row["status"] if row else "missing"
        print(f"[watcher] approval {approval_id} is {state} -- resuming incident {incident_id}")
        try:
            result = app.invoke(Command(resume=approval_id), config)
            _handle_result(incident_id, app, config, result)
        except Exception as exc:
            print(f"[watcher] incident {incident_id} raised on resume {type(exc).__name__}: {exc}")
    return resumed


def run_forever() -> None:
    """Poll forever, starting from a floor set once at startup.

    The floor is read here, not inside poll_once, so it is fixed for the life
    of the process: once an incident above it has been investigated it drops
    out of list_unhandled_incidents on its own (it now has audit_log rows),
    and nothing here needs to raise the floor to keep up with that.
    """
    safety_policy.load_config()
    floor = max_incident_id()
    print(
        f"[watcher] polling for unhandled incidents every {POLL_INTERVAL_SECONDS}s "
        f"(ignoring incident {floor} and everything before it)"
    )
    while True:
        resume_answered()
        poll_once(floor)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        run_forever()
    finally:
        close_persistence()
