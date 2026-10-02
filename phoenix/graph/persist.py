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
every public function here degrades instead of raising, and says once per cause
what it could not do. What that buys is a run that finishes on time with its
findings intact. What it costs is that a run can end with no trail at all, and
the only evidence of that is a line on stdout -- which is why the diagnostic
names the cause, and why it is not optional to read.

evidence.source is written as the observer's tool name exactly as TOOL_DISPATCH
spells it. 002_evidence_hypotheses_audit.sql constrained that column to
('prometheus', 'loki', 'docker'); 004_evidence_source_widen.sql widens it to the
five tool names the observer actually uses. Until that migration has run on the
database, its CHECK rejects every row this module writes -- a migration to
apply, not a write to retry, and a run in that state still finishes on time with
no trail at all. Apply it with:

    docker compose exec -T postgres psql -U phoenix -d phoenix -f /docker-entrypoint-initdb.d/004_evidence_source_widen.sql

Running that by hand is the normal case rather than the exception. Everything
mounted into /docker-entrypoint-initdb.d executes only when the postgres-data
volume is first created, so a volume that predates the file never sees it and the
constraint on evidence.source is still the old one -- the file sitting on disk
says nothing about whether the database has run it. The error printed for a
refused row names the constraint, which is how a run tells a migration to apply
apart from a write to retry.

Every table written here also foreign-keys to incidents(id) -- 002 declares
REFERENCES incidents(id) on evidence, hypotheses and audit_log -- and nothing
here creates that row or the table. The incident belongs to the API, which
deduplicates an alert into it; the graph is only ever handed its id, and
graph.py defaults that id to 1. So on a database with no incident 1 every insert
above is refused, the run finishes on time anyway, and what it leaves is a
[persist] line per table it tried to write -- each naming the foreign key it
tripped -- over a trail with no rows in it. That reads as an investigation that
found nothing and recorded nothing, which is why it is written down here. Check
for the row with:

    docker compose exec -T postgres psql -U phoenix -d phoenix -c "SELECT id FROM incidents WHERE id = 1"

and create it with:

    docker compose exec -T postgres psql -U phoenix -d phoenix -c "INSERT INTO incidents (id, service_name, alertname, severity, raw_payload) VALUES (1, 'checkout-service', 'ServiceDown', 'critical', '{}'); SELECT setval('incidents_id_seq', (SELECT MAX(id) FROM incidents))"

001 gives every other column a default -- status, first_seen_at, last_seen_at,
alert_count -- so an INSERT has to name only the four that are NOT NULL with no
default behind them, and timestamps are the server's to keep. The id is pinned so
the graph's default reaches this row, and the setval beside it is not optional
next to a pinned id:
incidents_id_seq is where the API's own INSERT takes its id from, so a pinned 1
that never reached the sequence would be handed out again and fail its own
primary key. If checkout-service already has an active ServiceDown incident,
001's partial unique index means that row is the one to pass as the graph's first
argument instead.
"""

import os
from collections.abc import Hashable
from datetime import datetime

from phoenix.graph.schemas import ScoredHypothesis

DATABASE_URL_ENV = "DATABASE_URL"
POOL_MIN_SIZE = 1
POOL_MAX_SIZE = 5
POOL_WAIT_SECONDS = 2.0

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
_reported: set[Hashable] = set()


def _failure_key(table: str, exc: BaseException) -> tuple[str, type[BaseException], str]:
    """What makes one failed write a different failure from the one before it.

    A run writes an evidence row per tool call and an audit row per node pass,
    so a database that is down fails dozens of times for a single reason, and a
    line per failure would bury the reason in its own symptom. Keying on the
    table alone silences that, but it over-silences: the first CHECK violation on
    evidence.source would be the last word on that table for the rest of the
    run, and a later disconnection would go unnamed, leaving a trail that is
    partial with no hint of what stopped it.

    The exception's class and its message say whether this is the same failure
    arriving again or a new one, and the row is never part of the answer -- a
    row-per-row key would print every failure and say nothing.
    """
    return (table, exc.__class__, str(exc))


def _diagnose(once: Hashable, reason: str) -> None:
    """Say a persistence problem once per cause, not once per row.

    once is a failure key from _failure_key, or the pool's own message when the
    pool could never be built: either way it names a cause rather than a row.
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

    The wait for a connection is capped at POOL_WAIT_SECONDS rather than left at
    the driver's thirty seconds. A server that is up but refusing connections is
    discovered at checkout, not at open(), and a run writes a row per tool call;
    at the default it would spend half a minute on each one before printing the
    same line, which is the opposite of finishing on time.
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
            url,
            min_size=POOL_MIN_SIZE,
            max_size=POOL_MAX_SIZE,
            open=False,
            timeout=POOL_WAIT_SECONDS,
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


def get_incident_first_seen(incident_id: int) -> str | None:
    """When this incident was first observed, or None if it cannot be read.

    A read rather than a write, because the deployment correlation needs the
    incident's onset and must not create a row in order to learn it. Same
    no-pool-means-no-trail posture as the writers: a run without a database still
    diagnoses, it just cannot say whether a deploy preceded the incident, and
    the correlation verdicts say so rather than guessing.
    """
    pool = _get_pool()
    if pool is None:
        return None
    try:
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT first_seen_at FROM incidents WHERE id = %(incident_id)s",
                {"incident_id": incident_id},
            ).fetchone()
    except Exception as exc:  # noqa: BLE001 - an unreadable clock is not a failed run
        _diagnose(exc, f"could not read first_seen_at for incident {incident_id}: {exc}")
        return None
    return row[0].isoformat() if row and row[0] else None


def _execute(sql: str, params: dict) -> None:
    """Run one INSERT, committed, or do nothing if there is no pool to run it.

    The writers ask for the pool before they build their parameters, so a run
    with nowhere to write never reaches for the driver at all. The check is
    repeated here because a pool closed in between is still no pool.

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

    The pool is asked for before the parameters are built, so a run with no
    database reports the one true cause -- an unset URL, or no driver -- rather
    than a missing import raised while wrapping raw_data for a write that had
    nowhere to go.

    Never raises.
    """
    try:
        if _get_pool() is None:
            return
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
            _failure_key("evidence", exc),
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

    One failed row does not abandon the rest of the ranking, for the same reason
    a malformed tool call does not take down the observer's batch: a batch is
    worth more than any one of its entries.

    Never raises.
    """
    if not scored_hypotheses:
        return

    if _get_pool() is None:
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
            _diagnose(_failure_key("hypotheses", exc), f"hypothesis row not written: {exc}")


def record_audit(
    incident_id: int,
    node: str,
    event_type: str,
    detail: dict,
    reasoning_text: str | None,
) -> None:
    """Append one audit row: which node decided what, and the state it saw.

    created_at is left to the column default, so the row records when the
    database wrote it rather than when the graph believed it did.

    detail is handed over unchanged, which is what makes this the place a
    routing decision belongs: nothing here reads a confidence, recomputes one,
    or adjusts one. The value in a row is there because deterministic code
    produced it upstream, and this module's only opinion is that the row is
    worth keeping.

    Never raises.
    """
    try:
        if _get_pool() is None:
            return
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
            _failure_key("audit_log", exc),
            f"audit row for {node}/{event_type} not written: {exc}",
        )


def close_persistence() -> None:
    """Close the pool this module opened, if it ever opened one.

    The graph is a one-shot process that builds the pool lazily on its first
    write, so the pool is the only thing here that outlives a function call and
    the only thing a caller has to hand back.

    Closing is not permanent, but it is not a reset either. After a plain close
    the module still treats the database as reachable, so a later write builds a
    fresh pool. A pool that could never be built is the exception: _get_pool
    short-circuits on the recorded _pool_error and nothing here clears it, so a
    process whose database was unreachable stays without a trail for the life of
    the process instead of rebuilding a pool on every row it still has left to
    write. That latch is deliberate -- it is what keeps a degraded run quiet --
    and it is why close_persistence cannot be relied on to forgive a database
    that was already gone. Call it when the run is over, not to disable
    persistence.
    """
    global _pool
    pool, _pool = _pool, None
    if pool is not None:
        pool.close()
