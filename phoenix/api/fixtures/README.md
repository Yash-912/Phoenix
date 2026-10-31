# Dashboard fixtures

**These are not real database rows.** They are shaped from the Gate 0 run dumps and
from the graph code, for developing and testing the dashboard without the live stack.

Each file is one incident, holding the rows the API reads for it:
`incident`, `evidence`, `hypotheses`, `audit`, `approvals`, `memory` (an
`incident_memory` row or `null`). Column names and types follow
`phoenix/db/init/001-008`, with timestamps as ISO strings.

What is real and what is composed:

- The run dumps only hold `(node, event_type, reasoning_text)` for each audit row, plus
  approval and memory summaries. Reasoning text in these fixtures is copied from them
  where a real run had it (the rollback reasoning, the verification sentence, the
  memory-match sentence, the insufficient-evidence summary, the PR-duplicate line).
- Evidence rows, hypothesis rows, score breakdowns and `detail` objects are composed to
  match the shapes the graph writes (`record_audit` calls in `phoenix/graph/nodes.py`,
  `graph.py`, `tier3_nodes.py`, `memory.py`, `watcher.py`). They show the right fields,
  not real measurements.
- Incident ids, timestamps, PR numbers and URLs are made up. The PR URLs point at
  `example/lab`.

| file | outcome it shows |
| --- | --- |
| `resolved_rollback.json` | Tier 2 rollback, approval granted, verified, resolved, memory match |
| `pr_exists.json` | Tier 3: an open PR already carries the fix, unused branch deleted |
| `pr_opened_after_restart.json` | Tier 1 restart verified, then Tier 3 PR opened, branch kept |
| `insufficient_evidence.json` | a failed read, no hypothesis with two sources, an evidence report |
| `approval_denied.json` | an approver denied the request, escalated |
| `approval_expired.json` | nobody answered, the request expired, escalated |
| `awaiting_approval.json` | a request still open, paused for an approver |
| `llm_unavailable.json` | the model could not be reached, no memory row |
| `action_unavailable.json` | a cause with no allowed action |
| `investigation_failed.json` | the investigation crashed repeatedly and was given up on |

They are checked against the live database after Gate 0 finishes.
