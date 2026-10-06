import re
import sys
import time
from datetime import datetime, timezone

from langgraph.graph import END
from langgraph.types import Command

from phoenix.graph import investigation, scoring, verification
from phoenix.graph.llm_client import decide_hypotheses, decide_tool_calls
from phoenix.graph.persist import (
    get_incident_first_seen,
    record_audit,
    record_evidence,
    record_hypotheses,
)
from phoenix.graph.remediation_dispatch import dispatch
from phoenix.graph.remediation_policy import TIER_2_ACTIONS, is_tier3_eligible, plan_action
from phoenix.graph.schemas import ScoredHypothesis
from phoenix.graph.state import AgentState
from phoenix.tools import deploy_tool
from phoenix.tools.deploy_tool import get_recent_deployments
from phoenix.tools.docker_tool import get_container_state
from phoenix.tools.health_tool import inspect_health
from phoenix.tools.loki_tool import query_loki
from phoenix.graph.symptom import blocks_action, current_symptom
from phoenix.tools.latency_tool import query_prometheus_with_latency_measure


def _say(message: str = "") -> None:
    """Print progress, losing only the characters this console cannot encode.

    Most of what gets printed here is the model's own words: hypothesis
    descriptions, tool arguments, plan reasoning. A live model writes typographic
    hyphens and em dashes as a matter of course, and a stock Windows console
    (cp1252) raises UnicodeEncodeError on them from inside the node -- which
    aborts the whole run over a character no operator could act on. The diagnosis
    is worth far more than the punctuation, so the odd character is escaped and
    the run continues.
    """
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    # pyflakes: noqa - the fallback below is the readable path for a stdout that
    # claims an encoding it cannot honour.
    try:
        rendered = message.encode(encoding, "backslashreplace").decode(encoding, "replace")
    except (LookupError, UnicodeError):
        rendered = message.encode("ascii", "backslashreplace").decode("ascii")
    print(rendered)


# The ONLY tools the LLM's decisions can ever result in executing.
# Observer stays read-only: remediation actions (restart/pause/cache)
# live in remediation_tool.py and are executor-only, never dispatched here.
TOOL_DISPATCH = {
    "query_prometheus": lambda args: query_prometheus_with_latency_measure(args["promql"]),
    "query_loki": lambda args: query_loki(args["logql"], args.get("minutes", 15)),
    "get_container_state": lambda args: get_container_state(args["container_name"]),
    "inspect_health": lambda args: inspect_health(args["service_name"]),
    "get_recent_deployments": lambda args: get_recent_deployments(
        args["service_name"], args.get("limit", 10)
    ),
}

# needs_evidence entries are LLM-authored free text bound for another model's prompt.
MAX_EVIDENCE_REQUESTS = 6
MAX_REQUEST_LENGTH = 120
MIN_REQUEST_WORD = 4
REQUEST_STOP_WORDS = {
    "about", "after", "also", "and", "any", "are", "been", "being", "but", "check",
    "confirm", "could", "did", "does", "evidence", "for", "from", "had", "has", "have",
    "into", "its", "just", "look", "looking", "more", "most", "must", "need", "needs",
    "only", "our", "refute", "see", "should", "show", "signal", "some", "such", "than",
    "that", "their", "them", "then", "there", "these", "they", "this", "those", "very",
    "want", "wants", "were", "what", "when", "where", "which", "while", "will",
    "with", "would", "you", "your",
}

_WORD = re.compile(r"[a-z0-9]+")


def _returned_text(payload) -> str:
    """A tool's payload flattened to text, field names left out.

    Every payload is a shape before it is a finding: docker's State, Loki's
    result, the status/text envelope the lab tools share. A field name describes
    the shape, which is why scoring._content_values drops them too, and a request
    must not retire against one.
    """
    if isinstance(payload, dict):
        return " ".join(_returned_text(value) for value in payload.values())
    if isinstance(payload, (list, tuple)):
        return " ".join(_returned_text(value) for value in payload)
    return "" if payload is None else str(payload)


def _evidence_words(evidence: list[dict]) -> set[str]:
    """Every word a tool has actually returned, as a set.

    Matched as whole words, never as substrings: "exit" must not be satisfied by
    "exits", or a request retires against text nobody wrote.

    A read that failed contributes nothing. What a failure carries is the
    transport's own complaint -- "Connection refused", "Read timed out" -- and
    those are words like any other, so counting them lets an unanswered request
    retire against the news that it could not be answered. scoring._is_usable is
    the same predicate the scorer uses to keep a failure from satisfying a
    hypothesis and suppressing its contradiction penalty; a request and a
    hypothesis are both claims about what was learned, and a failed read is
    evidence that neither can rest on.

    The summary is excluded outright. It is "tool(arguments)", and the arguments
    repeat verbatim every iteration -- inspect_health is called with
    {'service_name': 'checkout-service'} on nearly every one of them -- so a
    request naming "service name" would retire against the call rather than the
    result. What was asked is not what was learned; only raw_data counts.
    """
    words: set[str] = set()
    for item in evidence:
        if not scoring._is_usable(item):
            continue
        words.update(_WORD.findall(_returned_text(item.get("raw_data")).lower()))
    return words


def _content_words(text: str) -> set[str]:
    """The words in a request that actually name a signal: long enough to mean
    something, and not a connector, not the instruction verb the model wrapped the
    request in, and not the meta-vocabulary it wrapped it in.
    """
    return {word for word in _WORD.findall(text.lower())
            if len(word) >= MIN_REQUEST_WORD and word not in REQUEST_STOP_WORDS}


def _is_answered(request: str, read: set[str]) -> bool:
    """True when what the tools returned already says everything this request asks for.

    Deliberately literal, and therefore deliberately conservative: a request is
    retired only when every content word in it is already in the returned data.
    Keeping one the observer has in fact satisfied costs a redundant read at
    worst, and the prompt tells it to discount a request the evidence already
    covers. Retiring one nobody answered costs evidence nobody can get back, and
    once the router gates on an empty list it ends the investigation outright.
    """
    words = _content_words(request)
    return bool(words) and words <= read


def _pending_evidence_requests(
    hypotheses: list[ScoredHypothesis], evidence: list[dict]
) -> list[str]:
    """The confirm/refute signals the surviving hypotheses still want, best-ranked
    first, with the ones the returned results already answer retired.

    Also where the list is bounded: LLM-authored free text is trimmed to
    MAX_REQUEST_LENGTH and the list stops at MAX_EVIDENCE_REQUESTS, so neither the
    prompt nor the state a later router reads can be swollen by it.
    """
    read = _evidence_words(evidence)
    requests: list[str] = []

    for scored in hypotheses:
        for requested in scored.hypothesis.needs_evidence:
            text = requested.strip()
            if not text or _is_answered(text, read):
                continue
            text = text[:MAX_REQUEST_LENGTH]
            if text not in requests:
                requests.append(text)
            if len(requests) == MAX_EVIDENCE_REQUESTS:
                return requests

    return requests


def _diagnosis_line(scored: ScoredHypothesis) -> str:
    """One scored hypothesis as a single line of prose.

    The family the LLM named, the score code gave it, and the LLM's own
    description of it. Printed for the operator and stored as the diagnoser's
    reasoning_text, so the audit row and the console say the same thing.
    """
    return (
        f"{scored.hypothesis.category} scores {scored.score:.2f}: "
        f"{scored.hypothesis.description}"
    )


def observer_node(state: AgentState) -> AgentState:
    """Slice 2.3, completed: the LLM decides which tool(s) to call next;
    this code executes exactly what it decides and nothing else. The diagnoser's
    outstanding evidence requests ride along into that decision, so the loop
    investigates what the hypotheses actually want confirmed.

    Whatever the LLM spent asking is added to state.tokens_spent, including on
    the turn it asks for nothing: the call was made and billed either way.

    Every tool call that ran leaves an evidence row, and the pass leaves one
    audit row naming what the LLM asked for, what the allowlist actually let
    run, and what the pass cost. The evidence row is written from the item that
    went onto the state, so the row and the state are the same fact.

    The LLM's arguments are checked only for being JSON, never for carrying the
    keys or the types a tool needs, so a call the tool cannot accept dies inside
    the tool: a string where the tool wants an int, a required key that never
    arrived. The pass is caught around each call rather than around the loop,
    because one unreadable argument must not cost the operator the run -- the
    investigation is over and the diagnosis is in this state, and an exception
    here unwinds past the print of the final state to lose all of it.

    A call that raised is recorded like any other call, in the same
    {"status": "error", "error": ...} envelope the tools themselves write when a
    read fails: the trail should carry the fact that a read was attempted and
    did not come back, which is worth more to whoever reads it afterwards than a
    gap, and that envelope is the one scoring._is_usable reads, so a recorded
    failure is recorded and is not evidence. The audit row names the calls that
    raised in failed_tools, so no row claims a read returned data when it
    returned nothing.

    This node mutates its argument and returns it, which the two nodes added
    after it deliberately do not do. The distinction is the return value: a node
    that returns the state hands langgraph the whole object, so an in-place
    change is part of what gets persisted. A node that returns a Command without
    an update does not, so an in-place change there would be written onto a copy
    the run never sees. Both shapes are correct in their own place and only one
    of them is, which is why every routing node in this module returns its
    changes in Command(update=...) and this one does not route.
    """
    state.iteration += 1

    # The incident's onset is read once per pass and handed to the tools that
    # compare timestamps against it. Read here rather than inside deploy_tool
    # because persist is this layer's concern, and because a failure to read it
    # must not stop the run: the correlation degrades to "unknown" and the rest
    # of the investigation proceeds on the evidence that is readable.
    deploy_tool.set_incident_started_at(get_incident_first_seen(state.incident_id))

    # What the leading hypothesis rests on and what has not been tried, computed
    # in code. The first pass has no leader to report on, so its call is exactly
    # the one it always was.
    evidence_state = investigation.build_evidence_state(state)
    decision = decide_tool_calls(
        state.service_name,
        state.evidence,
        state.needs_evidence,
        **({"evidence_state": evidence_state} if evidence_state else {}),
    )
    state.tokens_spent += decision.tokens
    requested_calls = decision.calls
    dispatched: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []
    already_run = {item["summary"] for item in state.evidence}

    if not requested_calls:
        _say(f"[observer] iteration {state.iteration}: LLM requested no tool calls")

    for call in requested_calls:
        tool_name = call["name"]
        tool_fn = TOOL_DISPATCH.get(tool_name)
        if tool_fn is None:
            _say(f"[observer] iteration {state.iteration}: ignoring unrecognized tool '{tool_name}'")
            continue

        # The same string the evidence row's summary carries, so a call counts as
        # a repeat exactly when an identical one has already been recorded --
        # including one proposed twice in this very pass. Exact repeats only: a
        # different query that returns the same nothing is judged by the
        # diagnoser's progress check, not here.
        call_summary = f"{tool_name}({call['arguments']})"
        if call_summary in already_run:
            skipped.append(call_summary)
            _say(f"[observer] iteration {state.iteration}: skipping {call_summary}, already run")
            continue
        already_run.add(call_summary)

        failure = None
        try:
            result = tool_fn(call["arguments"])
        except Exception as exc:
            failure = f"{type(exc).__name__}: {exc}"
            result = {"status": "error", "error": failure}
            failed.append(tool_name)

        item = {
            "iteration": state.iteration,
            "source": tool_name,
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "summary": f"{tool_name}({call['arguments']})",
            "raw_data": result,
        }
        state.evidence.append(item)
        record_evidence(state.incident_id, item)
        dispatched.append(tool_name)
        if failure is None:
            _say(f"[observer] iteration {state.iteration}: called {tool_name}({call['arguments']})")
        else:
            _say(
                f"[observer] iteration {state.iteration}: {tool_name}({call['arguments']}) "
                f"failed: {failure}"
            )

    record_audit(
        state.incident_id,
        "observer",
        "observer_pass",
        {
            "iteration": state.iteration,
            "requested_tools": [call["name"] for call in requested_calls],
            "dispatched_tools": dispatched,
            "failed_tools": failed,
            "skipped_duplicates": skipped,
            "observation_exhausted": not dispatched,
            "evidence_collected": len(state.evidence),
            "tokens_spent": state.tokens_spent,
        },
        None,
    )

    # No call this pass was one it had not already run: either the model asked
    # for nothing or only for repeats. The router reads this after a stagnant
    # pass as "there is nothing further to observe".
    state.observation_exhausted = not dispatched

    return state


def diagnoser_node(state: AgentState) -> AgentState:
    """The LLM describes candidate root causes; scoring.py alone decides how much
    to believe them. Confidence is the top ranked hypothesis' deterministic score,
    so it moves with the evidence the observer collected, never with a count of
    evidence items and never with anything the model asserted about itself. The
    ranked hypotheses' needs_evidence entries become the state's outstanding
    requests, minus any the evidence already answers: empty means there is nothing
    left to go and look at.

    The LLM's own tokens land on state.tokens_spent here too, covering both
    calls when a structured attempt fell back to prompt JSON.

    The whole ranking is written to the hypotheses table, one row per entry and
    stamped with the pass that produced it, and the pass leaves an audit row
    carrying the confidence and every hypothesis' category -- the table has no
    category column, so this is where a family survives. Nothing here recomputes
    a score: the row is the scorer's answer written down, not a second opinion.
    """
    decision = decide_hypotheses(state.service_name, state.evidence)
    state.tokens_spent += decision.tokens
    proposed = decision.output.hypotheses

    if not proposed:
        _say(f"[diagnoser] iteration {state.iteration}: LLM proposed no hypotheses")
        state.hypotheses = []
        state.needs_evidence = []
        state.confidence = 0.0
    else:
        state.hypotheses = [
            ScoredHypothesis(hypothesis=hypothesis, score=score, score_breakdown=breakdown)
            for hypothesis, score, breakdown in scoring.score_all(state.evidence, proposed)
        ]
        state.needs_evidence = _pending_evidence_requests(state.hypotheses, state.evidence)
        state.confidence = scoring.top_confidence(state.evidence, proposed)

        for scored in state.hypotheses:
            _say(f"[diagnoser] iteration {state.iteration}: {_diagnosis_line(scored)}")
        _say(f"[diagnoser] iteration {state.iteration}: confidence={state.confidence:.2f}")

    # Did this pass change anything the scorer sees about the leader? Read off
    # the scores just computed, never recomputed, and kept apart from them: it
    # decides nothing here, it only counts, and the router acts on the count.
    signature = investigation.progress_signature(state.hypotheses)
    progressed, progress_reasons = investigation.assess_progress(state.progress_signature, signature)
    state.stagnant_passes = 0 if progressed else state.stagnant_passes + 1
    state.progress_signature = signature
    if not progressed:
        _say(
            f"[diagnoser] iteration {state.iteration}: evidence state unchanged "
            f"(stagnant pass {state.stagnant_passes})"
        )

    record_hypotheses(state.incident_id, state.hypotheses, iteration=state.iteration)
    record_audit(
        state.incident_id,
        "diagnoser",
        "diagnosis",
        {
            "iteration": state.iteration,
            "confidence": state.confidence,
            "confidence_threshold": state.confidence_threshold,
            "tokens_spent": state.tokens_spent,
            "progress": {
                "progressed": progressed,
                "reasons": progress_reasons,
                "stagnant_passes": state.stagnant_passes,
                "signature": signature,
            },
            "hypotheses": [
                {
                    "description": scored.hypothesis.description,
                    "category": scored.hypothesis.category,
                    "score": scored.score,
                }
                for scored in state.hypotheses
            ],
        },
        "\n".join(_diagnosis_line(scored) for scored in state.hypotheses) or None,
    )

    return state


def _escalate(
    state: AgentState,
    event_type: str,
    reason: str,
    print_line: str,
    extra: dict | None = None,
    node: str = "remediator",
) -> Command:
    """Stop the run with a named reason, on the audit trail before it stops.

    A mutating step that fails is the one place a run must not try again on its
    own, so every refusal here ends the run rather than routing back to the
    observer. The reason is written both to the trail and into the final state:
    the row is what an operator reads afterwards and the field is what the
    caller reads immediately, and two different strings for one event is how a
    postmortem ends up arguing with itself.

    extra carries whatever else the caller needs to survive into the final
    state. The verifier passes its verdict this way, because an escalation whose
    final state does not say what the check actually found is the one row an
    operator has to act on with no information attached to it.

    node is a parameter rather than a constant because the verifier escalates
    through here too. Filing its rows under 'remediator' would print and record
    a node that never ran the pass, immediately after the verifier wrote a row
    correctly attributed to itself -- a trail that contradicts itself about who
    did what, which is the one thing a trail exists to prevent.
    """
    _say(f"[{node}] {print_line} -> end (escalate)")
    record_audit(
        state.incident_id,
        node,
        event_type,
        {
            "iteration": state.iteration,
            "remediation_attempts": state.remediation_attempts,
            "max_remediation_attempts": state.max_remediation_attempts,
            "policy_mode": state.policy_mode,
            "reason": reason,
        },
        reason,
    )
    return Command(
        goto=END,
        update={"status": "escalated", "escalation_reason": reason, **(extra or {})},
    )


def _check_symptom_before_acting(state: AgentState, category: str | None) -> Command | None:
    """Stop the run if the symptom this finding describes is no longer observed.

    The verifier will not report a recovery it did not observe; this will not act on
    a symptom that is gone. A finding can be well supported by evidence that is real
    and old -- a slowdown, a memory climb -- for a service that has since cleared, and
    a restart or a patch for that repairs nothing. Only a clear absence stops the run:
    a symptom that cannot be observed (no traffic, a failed read) is not one that is
    gone, and the finding's evidence already had to overlap the incident's onset.

    The run ends through the same escalation every other refusal here uses, with the
    reason on the trail, rather than as resolved: nothing was observed to recover.
    A category with no probe is not checked, and no row is written for it.
    """
    observed = current_symptom(category, state.service_name)
    if observed.get("state") == "not_checked":
        return None
    if blocks_action(observed):
        reason = (
            f"the {category} symptom on {state.service_name} is no longer observed "
            f"({_describe_observation(observed)}), so no action was taken on the earlier finding"
        )
        return _escalate(state, "symptom_cleared", reason, reason, extra={"symptom_check": observed})
    record_audit(
        state.incident_id,
        "remediator",
        "symptom_checked",
        {"iteration": state.iteration, "category": category, "observed": observed},
        f"the {category} symptom is {observed.get('state')} on {state.service_name}",
    )
    return None


def _describe_observation(observed: dict) -> str:
    """The numbers a probe measured, as a short phrase for the reason."""
    parts = [f"{name}={value}" for name, value in observed.items() if name != "state"]
    return ", ".join(parts) or "healthy on the latest reading"


def _restart_already_applied(state: AgentState, plan, pre_action_signal) -> dict | None:
    """Evidence that repeating this action cannot free anything more, or None.

    A failed verification records the level the action left the process at. If
    the same action is about to be taken again and the process is still at that
    level -- it has not grown past the allocator noise the verifier itself
    tolerates -- then the process is already in the state the action produces,
    and a second identical action would only reproduce it: the next
    verification would compare that state with itself.

    This decides nothing about whether the first attempt worked; the verifier's
    verdict stands and the run is never called recovered here. It only declines
    to repeat an action that has nothing left to do. Memory that has grown past
    the noise since, or a previous result that recorded no level (any check that
    does not read one), leaves the retry exactly as it was.
    """
    prior = state.verification_result
    if not isinstance(prior, dict) or prior.get("outcome") != verification.OUTCOME_FAIL:
        return None
    if prior.get("action") != plan.action or prior.get("check") != plan.check:
        return None
    detail = prior.get("detail")
    after = detail.get("after") if isinstance(detail, dict) else None
    current = pre_action_signal.get("bytes") if isinstance(pre_action_signal, dict) else None
    for value in (after, current):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
    if current > after + verification.NOISE_FLOOR_BYTES:
        return None
    return {
        "post_action_bytes": after,
        "current_bytes": current,
        "tolerance_bytes": verification.NOISE_FLOOR_BYTES,
    }


def remediator_node(state: AgentState) -> Command:
    """Take the one Tier 1 action this diagnosis allows, then hand off to verify.

    The order of the gates below is the design, not a convenience. The attempt
    cap is checked first, before the policy mode and before the snapshot, so
    that a state which arrives here already over budget -- by a misrouted edge or
    a hand-built state -- costs no tool call and no signal read. A cap enforced
    after the snapshot would still be bounded in what it does to the world, but it
    would not be bounded in what it spends doing nothing, and the failure this
    guards against is a loop that keeps restarting a service that cannot be
    restarted.

    guarded mode refuses rather than asks. There is no approval step in this
    phase: a run that stops here has taken no action and a human has been told
    why, which is the safe reading of "guarded" even though it is not yet the
    useful one.

    A category with no Tier 1 action ends the run as action_unavailable with no
    escalation reason. The diagnosis succeeded, the correct response was that a
    human should look, and filing that as an escalation would put a successful
    diagnostic in the same column as a failed one.

    The pre-action snapshot is read immediately before the dispatch, never after.
    That ordering is the whole basis of the comparison the verifier makes: a
    reading taken after a restart is a post-restart reading, and comparing it to
    a pre-restart snapshot would confirm that memory is where a fresh process
    always puts it. When the snapshot cannot be read the action still runs and
    the unreadable envelope is stored with it, because refusing to restart a
    crash-looping container when metrics happen to be down would abandon the
    service precisely when it is least likely to be healthy. The cost of that
    choice is that verification later reports inconclusive, which is the honest
    answer rather than a false pass.

    A dispatch that returns an error envelope or raises is not an attempt. The
    container was never restarted, so counting it would spend the cap on failures
    that changed nothing, and routing to the verifier would ask it to confirm a
    restart that did not happen.
    """
    if state.remediation_attempts >= state.max_remediation_attempts:
        reason = (
            f"remediation attempts exhausted "
            f"({state.remediation_attempts}/{state.max_remediation_attempts})"
        )
        return _escalate(state, "action_attempts_exhausted", reason, reason)

    if state.policy_mode == "guarded":
        category = state.hypotheses[0].hypothesis.category if state.hypotheses else "unknown"
        return _escalate(
            state,
            "action_blocked_by_policy",
            f"policy_mode is guarded, so no action was taken for the {category} finding",
            "policy_mode is guarded, so no action taken",
        )

    plan = plan_action(state)

    # Before anything is read, restarted or handed to Tier 3: is there still a
    # symptom to act on? A finding with no action to take has nothing to refuse.
    top_category = state.hypotheses[0].hypothesis.category if state.hypotheses else None
    if plan.available or is_tier3_eligible(top_category):
        stopped = _check_symptom_before_acting(state, top_category)
        if stopped is not None:
            return stopped

    if not plan.available:
        category = state.hypotheses[0].hypothesis.category if state.hypotheses else None
        if is_tier3_eligible(category):
            # slow_query's whole reason to exist in CATEGORY_ACTIONS as ():
            # there is no Tier 1/2 action, so the only honest next step is
            # handing the run to the Tier 3 subgraph rather than reporting
            # action_unavailable on a root cause that is in fact actionable,
            # just not by this table.
            reason = f"top hypothesis is {category}; no Tier 1/2 action applies, handing off to Tier 3"
            _say(f"[remediator] {reason} -> code_investigator")
            record_audit(
                state.incident_id, "remediator", "tier3_handoff",
                {"iteration": state.iteration, "category": category}, reason,
            )
            return Command(goto="code_investigator", update={"status": "tier3_investigating"})

        _say(f"[remediator] {plan.reasoning} -> end (action_unavailable)")
        record_audit(
            state.incident_id,
            "remediator",
            "action_unavailable",
            {
                "iteration": state.iteration,
                "category": category,
                "remediation_attempts": state.remediation_attempts,
            },
            plan.reasoning,
        )
        return Command(goto=END, update={"status": "action_unavailable"})

    pre_action_signal = verification.read_signal(plan.check, plan.container)

    # Declined before the dispatch, never after: a restart that was not taken
    # costs the world nothing, and the verifier's earlier verdict stays on the
    # state as it was. The category decides where the run goes next, through the
    # same policy that already routes a finding no Tier 1 action can fix.
    applied = _restart_already_applied(state, plan, pre_action_signal)
    if applied is not None:
        category = state.hypotheses[0].hypothesis.category if state.hypotheses else None
        reason = (
            f"{plan.action} on {plan.container} would repeat the last attempt: the process is "
            f"still at the {applied['post_action_bytes']:.0f} that attempt left "
            f"({applied['current_bytes']:.0f} now, within {applied['tolerance_bytes']} of it), "
            f"so another {plan.action} has nothing further to free"
        )
        if is_tier3_eligible(category):
            _say(f"[remediator] {reason} -> code_investigator")
            record_audit(
                state.incident_id, "remediator", "tier3_handoff",
                {"iteration": state.iteration, "category": category, "redundant_action": plan.action, **applied},
                reason,
            )
            return Command(
                goto="code_investigator",
                update={"status": "tier3_investigating", "tier1_mitigation": state.verification_result},
            )
        return _escalate(state, "action_redundant", reason, reason)

    action_at = datetime.now(timezone.utc).isoformat()

    _say(f"[remediator] taking {plan.action} on {plan.container} ({plan.reasoning})")
    try:
        result = dispatch(plan.action, plan.container, plan.args)
    except Exception as exc:
        reason = f"{plan.action} on {plan.container} raised {type(exc).__name__}: {exc}"
        return _escalate(state, "action_failed", reason, reason)

    if not isinstance(result, dict) or result.get("status") == "error":
        detail = (
            result.get("error")
            if isinstance(result, dict)
            else f"returned {type(result).__name__} rather than a result"
        )
        reason = f"{plan.action} on {plan.container} failed: {detail}"
        return _escalate(state, "action_failed", reason, reason)

    planned_action = {
        "action": plan.action,
        "container": plan.container,
        "check": plan.check,
        "pre_action_signal": pre_action_signal,
        "action_at": action_at,
        # What the action was supposed to achieve, so verification can check the
        # thing that was planned rather than inferring intent from the category.
        # A deploy rollback names the artifact it restored (to_version); a config
        # rollback names the value it restored (to_value). Tier 1 has neither.
        "expected_version": (
            (result.get("to_version") or result.get("to_value"))
            if plan.action in TIER_2_ACTIONS
            else None
        ),
    }
    record_audit(
        state.incident_id,
        "remediator",
        "action_executed",
        {
            "iteration": state.iteration,
            "action": plan.action,
            "container": plan.container,
            "check": plan.check,
            "action_at": action_at,
            "remediation_attempts": state.remediation_attempts + 1,
            "snapshot_usable": scoring._is_usable({"raw_data": pre_action_signal}),
            "result": result,
        },
        f"{plan.reasoning}; snapshot taken at {action_at}",
    )
    _say(
        f"[remediator] {plan.action} completed "
        f"(attempt {state.remediation_attempts + 1}/{state.max_remediation_attempts}) "
        f"-> verifier"
    )
    return Command(
        goto="verifier",
        update={
            "status": "confident",
            "planned_action": planned_action,
            "remediation_attempts": state.remediation_attempts + 1,
        },
    )


def verifier_node(state: AgentState) -> Command:
    """Did the action work? Then either finish the run or hand it back to look again.

    The delay before measuring is not politeness. A container in the first
    moments after a restart is still coming up: its working set is climbing
    toward steady state and its readiness probe has not necessarily run yet.
    Measuring immediately would grade the action on its own startup footprint,
    and for a memory category that means a fresh process's baseline being
    compared against the pre-restart snapshot -- which is the same reading the
    check is trying to make, arrived at for free.

    Three outcomes, and the middle one is the point of the module. A pass ends
    the run as resolved. A fail goes back to the observer for another look, but
    only while attempts remain; once the cap is spent there is nothing left to
    try and the run escalates. An inconclusive ends the run as escalated without
    waiting for the cap, because "could not check" is not a reason to spend the
    operator's next two attempts -- and crucially it is never a pass. A run that
    reports recovery it did not observe is worse than a run that reports nothing,
    because a human stops looking at the one that says everything is fine.

    The check is run against the snapshot the remediator took and the moment it
    took the action. Passing anything else would verify a different thing than
    the one that happened: the snapshot is what "before" means here, and
    action_at is what separates a restart this run caused from a container that
    happened to be up already.

    Arriving here with no planned action means the state was built wrong. It is
    escalated rather than passed, on the same reasoning as an inconclusive check:
    the absence of a check is not evidence that the service is fine.
    """
    planned = state.planned_action
    if not planned:
        return _escalate(
            state,
            "verification_failed",
            "the verifier was reached with no planned action, so there is "
            "nothing to check and no basis for calling the service recovered",
            "reached with no planned action to verify",
            node="verifier",
        )

    time.sleep(state.verification_delay_seconds)

    category = planned.get("check")
    # Positional, including for categories that ignore the fifth argument: the
    # existing test doubles stand in for run_check as a single callable, and a
    # keyword here would be a change to their contract rather than to this one.
    outcome, detail = verification.run_check(
        category,
        state.service_name,
        planned.get("pre_action_signal") or {},
        planned.get("action_at"),
        planned.get("expected_version"),
    )

    verification_result = {
        "outcome": outcome,
        "check": category,
        "action": planned.get("action"),
        "detail": detail,
        "remediation_attempts": state.remediation_attempts,
        "max_remediation_attempts": state.max_remediation_attempts,
    }
    record_audit(
        state.incident_id,
        "verifier",
        "verification",
        verification_result,
        detail.get("reason") if isinstance(detail, dict) else None,
    )

    if outcome == verification.OUTCOME_PASS:
        if is_tier3_eligible(category):
            # Scenario 3's exact shape: the restart genuinely mitigated the
            # symptom, but memory_leak's root cause is still an unbounded
            # cache in worker-service's own code, which no restart touches.
            # tier1_mitigation preserves this pass as its own fact before
            # Tier 3 starts writing its own outcome onto the same state, so
            # the final record shows the mitigation AND the permanent fix,
            # never just the second overwriting the first.
            _say(f"[verifier] {category} check passed (Tier 1 mitigation) -> code_investigator for a permanent fix")
            return Command(
                goto="code_investigator",
                update={
                    "status": "tier3_investigating",
                    "verification_result": verification_result,
                    "tier1_mitigation": verification_result,
                },
            )
        _say(f"[verifier] {category} check passed -> end (resolved)")
        return Command(
            goto=END,
            update={"status": "resolved", "verification_result": verification_result},
        )

    if outcome == verification.OUTCOME_FAIL:
        if state.remediation_attempts < state.max_remediation_attempts:
            _say(
                f"[verifier] {category} check failed "
                f"(attempt {state.remediation_attempts}/{state.max_remediation_attempts}) "
                f"-> back to observer"
            )
            return Command(
                goto="observer",
                update={
                    "status": "investigating",
                    "verification_result": verification_result,
                },
            )
        reason = (
            f"the {category} check still fails after "
            f"{state.remediation_attempts}/{state.max_remediation_attempts} "
            f"attempts"
        )
        return _escalate(
            state,
            "verification_failed",
            reason,
            reason,
            extra={"verification_result": verification_result},
            node="verifier",
        )

    reason = (
        f"the {category} check could not confirm recovery: "
        f"{detail.get('reason') if isinstance(detail, dict) else detail}"
    )
    return _escalate(
        state,
        "verification_failed",
        reason,
        reason,
        extra={"verification_result": verification_result},
        node="verifier",
    )
