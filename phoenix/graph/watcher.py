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

from phoenix.graph.graph import build_graph
from phoenix.graph.persist import close_persistence, list_unhandled_incidents
from phoenix.graph.state import AgentState

POLL_INTERVAL_SECONDS = int(os.environ.get("PHOENIX_WATCH_INTERVAL_SECONDS", "10"))


def poll_once() -> list[int]:
    """Run every unhandled incident once. Returns the ids it started.

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
    for incident_id, service_name in list_unhandled_incidents():
        print(f"[watcher] incident {incident_id} ({service_name}) has no investigation yet -- starting one")
        started.append(incident_id)
        try:
            app = build_graph()
            result = app.invoke(AgentState(incident_id=incident_id, service_name=service_name))
            status = result.get("status") if isinstance(result, dict) else getattr(result, "status", None)
            print(f"[watcher] incident {incident_id} finished: status={status}")
        except Exception as exc:
            print(f"[watcher] incident {incident_id} raised {type(exc).__name__}: {exc}")
    return started


def run_forever() -> None:
    print(f"[watcher] polling for unhandled incidents every {POLL_INTERVAL_SECONDS}s")
    while True:
        poll_once()
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        run_forever()
    finally:
        close_persistence()
