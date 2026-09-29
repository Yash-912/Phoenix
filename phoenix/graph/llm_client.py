import json
import os

from openai import OpenAI

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
