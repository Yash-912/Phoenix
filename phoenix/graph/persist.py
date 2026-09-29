"""Durable trail for one investigation: evidence, scored hypotheses, audit rows.

The pool here is the graph's own and is opened on the first write, not at
import. phoenix.api.db is not imported and its pool is not reused: that pool
belongs to the API container's FastAPI lifespan, which the graph never runs,
and the plan keeps the two separate so neither service's database trouble is
the other's.

Every statement in this module is an INSERT. audit_log is append-only, and the
enforcement is the grant in 003_create_app_role.sh -- SELECT and INSERT, no
UPDATE, no DELETE -- not a promise made here. That grant is the reason this
file can be trusted on the subject; it is also the reason an UPDATE must never
be added to it, however reasonable the reason looks at the time.

A database that is missing, unreachable, or unhappy does not cost an
investigation. The diagnosis is the product; the trail is a record of it. So
every public function here degrades instead of raising, and says once per table
what it could not do. What that buys is a run that finishes on time with its
findings intact. What it costs is that a run can end with no trail at all, and
the only evidence of that is a line on stdout -- which is why the diagnostic
names the cause, and why it is not optional to read.

evidence.source is written as the observer's tool name exactly as TOOL_DISPATCH
spells it. 002_evidence_hypotheses_audit.sql constrained that column to
('prometheus', 'loki', 'docker'); 004_evidence_source_widen.sql widens it to the
five tool names the observer actually uses. Until that migration has run on the
database, its CHECK rejects every row this module writes -- a migration to
apply, not a write to retry.
"""

import os
from datetime import datetime

from phoenix.graph.schemas import ScoredHypothesis

DATABASE_URL_ENV = "DATABASE_URL"
POOL_MIN_SIZE = 1
POOL_MAX_SIZE = 5

INSERT_EVIDENCE = """
INSERT INTO evidence (incident_id, source, iteration, collected_at, summary, raw_data)
VALUES (%(incident_id)s, %(source)s, %(iteration)s, %(collected_at)s, %(summary)s, %(raw_data)s)
"""

INSERT_HYPOTHESIS = """
INSERT INTO hypotheses (incident_id, iteration, description, score, score_breakdown)
VALUES (%(incident_id)s, %(iteration)s, %(description)s, %(score)s, %(score_breakdown)s)
"""

INSERT_AUDIT = """
INSERT INTO audit_log (incident_id, node, event_type, detail, reasoning_text)
VALUES (%(incident_id)s, %(node)s, %(event_type)s, %(detail)s, %(reasoning_text)s)
"""

_pool = None
_pool_error: str | None = None
_reported: set[str] = set()


def _diagnose(once: str, reason: str) -> None:
    """Say a persistence problem once, not once per row.

    A run writes an evidence row per tool call and an audit row per node pass,
    so a database that is down fails dozens of times for one reason. A line per
    failure would bury the reason in its own symptom. The key is the table or
    the cause, never the row, so a genuine second failure is still reported.
    """
    if once in _reported:
        return
    _reported.add(once)
    print(f"[persist] {reason}")


def _get_pool():
    """This process's pool, built on the first write, or None if there is none.

    Built here rather than at import so that a run without DATABASE_URL, or
    without the driver installed, still imports and still reaches a diagnosis.
    Opened the way the API opens its own: constructed closed, opened once, then
    shared by every write for the life of the process.
    """
    global _pool, _pool_error
    if _pool is not None or _pool_error is not None:
        return _pool

    url = os.environ.get(DATABASE_URL_ENV, "").strip()
    if not url:
        _pool_error = f"{DATABASE_URL_ENV} is not set, so this run leaves no trail"
        _diagnose(_pool_error, _pool_error)
        return None

    try:
        from psycopg_pool import ConnectionPool

        pool = ConnectionPool(
            url, min_size=POOL_MIN_SIZE, max_size=POOL_MAX_SIZE, open=False
        )
        pool.open()
    except Exception as exc:
        _pool_error = f"no connection pool available, so this run leaves no trail: {exc}"
        _diagnose(_pool_error, _pool_error)
        return None

    _pool = pool
    return _pool


def _jsonb(value: dict):
    """The driver's own JSONB adapter, imported at the moment of a real write.

    psycopg3 will not put a dict into a jsonb column on its own, and there is no
    pool to hand the value to when the driver is not installed, so the import
    belongs here rather than at module level.
    """
    from psycopg.types.json import Jsonb

    return Jsonb(value)


def _execute(sql: str, params: dict) -> None:
    """Run one INSERT, committed, or do nothing if there is no pool to run it.

    Raises whatever the driver raised. The three writers below are the ones that
    decide what a failed write costs, and they all decide it the same way.
    """
    pool = _get_pool()
    if pool is None:
        return
    with pool.connection() as conn:
        conn.execute(sql, params)
        conn.commit()


def record_evidence(incident_id: int, evidence_item: dict) -> None:
    """Write the evidence row for one tool call the observer just made.

    Every field is the observer's own, read back out of the item it appended to
    state, so what the row says and what the scorer scored are the same facts.
    source is the tool name rather than a data-source family because that is
    what the observer records and what 004 widens the column to accept.

    collected_at is parsed out of the observer's ISO string into a real
    datetime, so the timestamptz column receives a timestamp rather than a
    string the server has to interpret.

    Never raises.
    """
    try:
        _execute(
            INSERT_EVIDENCE,
            {
                "incident_id": incident_id,
                "source": evidence_item["source"],
                "iteration": evidence_item["iteration"],
                "collected_at": datetime.fromisoformat(evidence_item["collected_at"]),
                "summary": evidence_item["summary"],
                "raw_data": _jsonb(evidence_item["raw_data"]),
            },
        )
    except Exception as exc:
        _diagnose(
            "evidence",
            f"evidence row from {evidence_item.get('source')!r} not written: {exc}",
        )


def record_hypotheses(
    incident_id: int, scored_hypotheses: list[ScoredHypothesis], *, iteration: int = 1
) -> None:
    """Write one row per scored hypothesis, in the order scoring ranked them.

    ScoredHypothesis is a model wrapped around another model, so the row is
    filled from attributes -- scored.hypothesis.description, scored.score, and
    the scorer's own breakdown. model_dump() carries the same values nested one
    level deeper, which would put a schema's shape into a statement that should
    not have to know one.

    The hypotheses table has no category column, so none is invented here: the
    category is recorded on the diagnoser's audit row, which has a free-form
    detail column to hold it.

    iteration is keyword-only because a ranked hypothesis carries no iteration
    of its own, and the column's default of 1 is a lie in a table that also
    timestamps rows -- a fourth pass recorded as the first is a trail that reads
    as if nothing happened before it.

    Never raises.
    """
    if not scored_hypotheses:
        return

    for scored in scored_hypotheses:
        try:
            _execute(
                INSERT_HYPOTHESIS,
                {
                    "incident_id": incident_id,
                    "iteration": iteration,
                    "description": scored.hypothesis.description,
                    "score": scored.score,
                    "score_breakdown": _jsonb(scored.score_breakdown),
                },
            )
        except Exception as exc:
            _diagnose("hypotheses", f"hypothesis row not written: {exc}")


def record_audit(
    incident_id: int,
    node: str,
    event_type: str,
    detail: dict,
    reasoning_text: str | None,
) -> None:
    """Append one audit row: which node decided what, and the state it saw.

    detail is stored exactly as it was handed over. Nothing here reads a
    confidence, recomputes one, or adjusts one: a value in this row is here
    because deterministic code produced it upstream, and this module's only
    opinion is that the row is worth keeping.

    created_at is left to the column default, so the row records when the
    database wrote it rather than when the graph believed it did.

    Never raises.
    """
    try:
        _execute(
            INSERT_AUDIT,
            {
                "incident_id": incident_id,
                "node": node,
                "event_type": event_type,
                "detail": _jsonb(detail),
                "reasoning_text": reasoning_text,
            },
        )
    except Exception as exc:
        _diagnose(
            "audit_log", f"audit row for {node}/{event_type} not written: {exc}"
        )


def close_persistence() -> None:
    """Close the pool this module opened, if it ever opened one.

    The graph is a one-shot process that builds the pool lazily on its first
    write, so the pool is the only thing here that outlives a function call and
    the only thing a caller has to hand back.
    """
    global _pool
    pool, _pool = _pool, None
    if pool is not None:
        pool.close()
