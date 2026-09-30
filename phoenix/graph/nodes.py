import re
from datetime import datetime, timezone

from phoenix.graph import scoring
from phoenix.graph.llm_client import decide_hypotheses, decide_tool_calls
from phoenix.graph.persist import record_audit, record_evidence, record_hypotheses
from phoenix.graph.schemas import ScoredHypothesis
from phoenix.graph.state import AgentState
from phoenix.tools.deploy_tool import get_recent_deployments
from phoenix.tools.docker_tool import get_container_state
from phoenix.tools.health_tool import inspect_health
from phoenix.tools.loki_tool import query_loki
from phoenix.tools.prometheus_tool import query_prometheus

# The ONLY tools the LLM's decisions can ever result in executing.
# Observer stays read-only: remediation actions (restart/pause/cache)
# live in remediation_tool.py and are executor-only, never dispatched here.
TOOL_DISPATCH = {
    "query_prometheus": lambda args: query_prometheus(args["promql"]),
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

    The summary is excluded outright. It is "tool(arguments)", and the arguments
    repeat verbatim every iteration — inspect_health is called with
    {'service_name': 'checkout-service'} on nearly every one of them — so a
    request naming "service name" would retire against the call rather than the
    result. What was asked is not what was learned; only raw_data counts.
    """
    words: set[str] = set()
    for item in evidence:
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
    """
    state.iteration += 1

    decision = decide_tool_calls(
        state.service_name, state.evidence, state.needs_evidence
    )
    state.tokens_spent += decision.tokens
    requested_calls = decision.calls
    dispatched: list[str] = []
    failed: list[str] = []

    if not requested_calls:
        print(f"[observer] iteration {state.iteration}: LLM requested no tool calls")

    for call in requested_calls:
        tool_name = call["name"]
        tool_fn = TOOL_DISPATCH.get(tool_name)
        if tool_fn is None:
            print(f"[observer] iteration {state.iteration}: ignoring unrecognized tool '{tool_name}'")
            continue

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
            print(f"[observer] iteration {state.iteration}: called {tool_name}({call['arguments']})")
        else:
            print(
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
            "evidence_collected": len(state.evidence),
            "tokens_spent": state.tokens_spent,
        },
        None,
    )

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
        print(f"[diagnoser] iteration {state.iteration}: LLM proposed no hypotheses")
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
            print(f"[diagnoser] iteration {state.iteration}: {_diagnosis_line(scored)}")
        print(f"[diagnoser] iteration {state.iteration}: confidence={state.confidence:.2f}")

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
