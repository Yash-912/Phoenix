import json
import os

from openai import (
    BadRequestError,
    ContentFilterFinishReasonError,
    LengthFinishReasonError,
    NotFoundError,
    OpenAI,
    OpenAIError,
    UnprocessableEntityError,
)
from pydantic import ValidationError

from phoenix.graph.schemas import DiagnoserOutput, Hypothesis

LLM_BASE_URL = os.environ["LLM_BASE_URL"]
LLM_API_KEY = os.environ["LLM_API_KEY"]
LLM_MODEL = os.environ["LLM_MODEL"]

client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "query_prometheus",
            "description": "Run a PromQL query against Prometheus for metrics evidence (CPU, memory, latency, error rate, service up/down).",
            "parameters": {
                "type": "object",
                "properties": {
                    "promql": {"type": "string", "description": "A valid PromQL query string."},
                },
                "required": ["promql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_loki",
            "description": "Run a LogQL query against Loki for log evidence from a container.",
            "parameters": {
                "type": "object",
                "properties": {
                    "logql": {"type": "string", "description": "A valid LogQL query string, e.g. {container=\"checkout-service\"}"},
                    "minutes": {"type": "integer", "description": "How many minutes back to search. Defaults to 15."},
                },
                "required": ["logql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_container_state",
            "description": "Inspect a container's current runtime state (running/restarting/exit code) via the docker-socket-proxy.",
            "parameters": {
                "type": "object",
                "properties": {
                    "container_name": {"type": "string", "description": "The exact container name, e.g. checkout-service."},
                },
                "required": ["container_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_health",
            "description": "Combined container state + app /health endpoint for a service.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service_name": {"type": "string", "description": "Service name, e.g. checkout-service."},
                },
                "required": ["service_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_deployments",
            "description": "Read recent JSON deployment markers for a service (image tag, commit, config, timestamp).",
            "parameters": {
                "type": "object",
                "properties": {
                    "service_name": {"type": "string", "description": "Service name, e.g. checkout-service."},
                    "limit": {"type": "integer", "description": "Max markers, newest first. Defaults to 10."},
                },
                "required": ["service_name"],
            },
        },
    },
]


def decide_tool_calls(service_name: str, evidence_so_far: list[dict]) -> list[dict]:
    """Ask the LLM which of the 5 allowlisted read-only tools to call next.

    Returns a list of {"name": str, "arguments": dict} — never executes anything itself.
    The LLM is ONLY ever offered these five tools (TOOL_SCHEMAS) — least-privilege
    allowlisting. It cannot request anything outside this list; remediation
    actions are executor-only and never offered here.
    """
    summary = [
        {"source": e["source"], "iteration": e["iteration"], "summary": e["summary"]}
        for e in evidence_so_far
    ]

    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are the Observer in an incident response system investigating "
                    f"service '{service_name}'. You may call any of the available tools "
                    "to gather evidence. Call only the tools you genuinely need next — "
                    "do not call a tool whose evidence you already have unless you need "
                    "a fresher read. You cannot take any remediation action; you can only "
                    "request evidence."
                ),
            },
            {
                "role": "user",
                "content": f"Evidence collected so far: {summary}\n\nWhich tool(s) do you want to call next?",
            },
        ],
        tools=TOOL_SCHEMAS,
        tool_choice="auto",
    )

    message = response.choices[0].message
    if not message.tool_calls:
        return []

    decided_calls = []
    for tc in message.tool_calls:
        try:
            arguments = json.loads(tc.function.arguments)
        except json.JSONDecodeError as exc:
            # The LLM's output is text that merely happens to usually be valid JSON —
            # it isn't guaranteed to be. One malformed call must not take down the
            # whole batch: discard just this one, keep any other valid calls in
            # the same response.
            print(f"[llm_client] discarding malformed tool call '{tc.function.name}': {exc}")
            continue
        decided_calls.append({"name": tc.function.name, "arguments": arguments})

    return decided_calls


def decide_hypotheses(service_name: str, evidence_so_far: list[dict]) -> DiagnoserOutput:
    """Ask the LLM for ranked root-cause hypotheses: descriptions only.

    Never executes anything and never calls a tool: the Observer's read-only
    allowlist stays the only thing in this system that can run anything.

    The preferred path is structured output: client.beta.chat.completions.parse
    with the DiagnoserOutput schema. Support for a json_schema response_format
    varies between OpenAI-compatible free-tier providers, so a rejected
    structured request falls back to asking for JSON in the prompt and
    validating it here with DiagnoserOutput.model_validate_json.

    The model is asked only to describe what might be wrong. It never states a
    score, a confidence, or a likelihood. Instead, phoenix/graph/scoring.py
    decides how much to believe a hypothesis, in pure code, from the evidence.

    Never raises: an unrecoverable LLM response yields an empty DiagnoserOutput.
    """
    summary = [
        {"source": e["source"], "iteration": e["iteration"], "summary": e["summary"]}
        for e in evidence_so_far
    ]

    messages = [
        {
            "role": "system",
            "content": (
                "You are the Diagnoser in an incident response system investigating "
                f"service '{service_name}'. Read-only evidence has been gathered; "
                "propose the root causes that evidence most plausibly supports, best "
                "first, at most 4 of them. For each one give a one-sentence "
                "description, the failure category (crash, overload, deploy, config, "
                "network, or unknown), and the signals that would confirm or refute "
                "it. Describe only, never state a score, a confidence, or a "
                "likelihood; those are computed in code from the evidence. You cannot "
                "take any remediation action."
            ),
        },
        {
            "role": "user",
            "content": f"Evidence collected so far: {summary}\n\nWhat are the most likely root causes?",
        },
    ]

    try:
        completion = client.beta.chat.completions.parse(
            model=LLM_MODEL,
            messages=messages,
            response_format=DiagnoserOutput,
        )
    except (
        BadRequestError,
        NotFoundError,
        UnprocessableEntityError,
        LengthFinishReasonError,
        ContentFilterFinishReasonError,
        ValidationError,
    ) as exc:
        print(f"[llm_client] structured hypotheses rejected, falling back to prompt JSON: {exc}")
    except OpenAIError as exc:
        print(f"[llm_client] hypothesis generation failed, LLM call failed: {exc}")
        return DiagnoserOutput.model_construct(hypotheses=[])
    else:
        parsed = completion.choices[0].message.parsed
        if parsed is not None:
            return parsed
        print("[llm_client] structured hypotheses came back unparsed, falling back to prompt JSON")

    try:
        fallback = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                *messages,
                {
                    "role": "user",
                    "content": (
                        "Reply with a single JSON object and nothing else, shaped as "
                        '{"hypotheses": [{"description": "<one sentence>", "category": '
                        '"crash|overload|deploy|config|network|unknown", '
                        '"needs_evidence": ["<signal>"]}]} holding 1 to 4 entries, best '
                        "first."
                    ),
                },
            ],
        )
    except OpenAIError as exc:
        print(f"[llm_client] discarding hypothesis generation, LLM call failed: {exc}")
        return DiagnoserOutput.model_construct(hypotheses=[])

    content = fallback.choices[0].message.content or ""

    try:
        return DiagnoserOutput.model_validate_json(content)
    except ValidationError as exc:
        print(f"[llm_client] hypothesis batch failed validation: {exc}")

    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        payload = None
        print(f"[llm_client] discarding unparsable hypothesis response: {exc}")

    hypotheses: list[Hypothesis] = []
    if isinstance(payload, dict) and isinstance(payload.get("hypotheses"), list):
        for item in payload["hypotheses"]:
            try:
                hypotheses.append(Hypothesis.model_validate(item))
            except ValidationError as exc:
                print(f"[llm_client] discarding malformed hypothesis: {exc}")
    if not hypotheses:
        print("[llm_client] no usable hypotheses in LLM response")
        return DiagnoserOutput.model_construct(hypotheses=[])

    return DiagnoserOutput(hypotheses=hypotheses[:4])
