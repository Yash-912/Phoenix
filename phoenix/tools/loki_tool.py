import os
import time

import requests

LOKI_URL = os.environ.get("LOKI_URL", "http://localhost:3100")

# A window longer than a day is not a diagnostic window, it is a download.
MAX_LOKI_MINUTES = 1440

# Enough of Loki's own error text to name the syntax problem, not a page of it.
MAX_ERROR_BODY_CHARS = 300


def query_loki(logql: str, minutes: int = 15) -> dict:
    """Query Loki for logs matching a LogQL query over the last `minutes` minutes.

    Read-only: only ever calls Loki's /loki/api/v1/query_range endpoint.

    `minutes` is validated before it is used in arithmetic, and it is validated as
    a *type* first. The annotation says int and the annotation is not enforced:
    the only caller is TOOL_DISPATCH, which forwards ``args.get("minutes", 15)``
    straight out of LLM-authored arguments, so a JSON string arrives here as str.
    "15" * 60 * 1_000_000_000 is string repetition, not multiplication, and it
    asks for roughly 900 GB before a single request goes out.

    The guard has to be here rather than around the call. ``except
    requests.RequestException`` cannot see this: a MemoryError is catchable, but
    under vm.overcommit_memory=1 the allocation draws an OOM-kill, which is
    SIGKILL and uncatchable by any handler in this process. Catching the error
    after the allocation is the one response to an OOM-kill that does not work.
    Booleans are rejected explicitly for the same reason -- isinstance(True, int)
    is True, so True would otherwise pass a bare int check and silently query a
    single minute.
    """
    if (
        isinstance(minutes, bool)
        or not isinstance(minutes, int)
        or not 1 <= minutes <= MAX_LOKI_MINUTES
    ):
        return {"status": "error", "error": f"invalid minutes: {minutes!r}"}

    now_ns = time.time_ns()
    start_ns = now_ns - (minutes * 60 * 1_000_000_000)

    try:
        response = requests.get(
            f"{LOKI_URL}/loki/api/v1/query_range",
            params={"query": logql, "start": start_ns, "end": now_ns, "limit": 100},
            timeout=5,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        message = str(exc)
        # A rejected query (400) says why in its body, and the caller is a model
        # that wrote the query: without Loki's own message the only thing it can
        # learn is that it failed, and it repeats the same mistake.
        body = getattr(getattr(exc, "response", None), "text", "") or ""
        if body:
            message = f"{message}: {body[:MAX_ERROR_BODY_CHARS]}"
        return {"status": "error", "error": message}
