-- Real persistence for payment-service's idempotency check (Scenario 2).
--
-- /charge must not double-charge a repeated order_id, so it needs to ask
-- "has this order_id been charged already?" before inserting a new row.
-- order_id is the PRIMARY KEY specifically so that question has an index to
-- answer it with -- the regression this schema supports is a query that
-- stops using that index, not a schema that never had one.
CREATE TABLE charges (
    order_id TEXT PRIMARY KEY,
    amount NUMERIC(10, 2) NOT NULL,
    status TEXT NOT NULL DEFAULT 'charged',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Seed enough historical rows that a full-table-scan-and-filter-in-Python
-- query is reliably, visibly slower than an indexed lookup -- the same
-- determinism requirement Phase 1 set for every other chaos scenario.
-- Measured, not assumed: at 20,000 rows the slow path (fetch everything,
-- loop in Python) was ~50ms against the fast path's ~2ms -- real, but well
-- under the 1s p95 HighLatency alert threshold (observability/prometheus/
-- alert.rules.yml). At 1,000,000 rows the same comparison measured ~2.0s
-- against ~18ms -- clears the threshold with comfortable margin, the same
-- "measured, not tuned" bar the memory-leak scenario's threshold margin was
-- held to.
INSERT INTO charges (order_id, amount, status, created_at)
SELECT
    'seed-order-' || i,
    (10 + (i % 500))::numeric / 100,
    'charged',
    now() - (interval '1 second' * i)
FROM generate_series(1, 1000000) AS i;
