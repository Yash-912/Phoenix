# Slack approval bridge: Spec

Date: 2026-11-01
Status: Approved by the owner on 2026-11-01, including every default under Open questions (now Resolved decisions). Nothing is implemented.
Source decisions: `docs/phoenix-v1-decisions.md` section 4 ("Slack: Socket Mode only ... first answer wins ... dashboard is the backup path ... update the Slack message with who decided and when"). This spec adds to that section and does not reopen it.

## Objective

Let a named approver approve or deny a held Tier 2 action from a Slack message, with the same guarantees as the dashboard: one approver list, first answer wins, a late answer is refused, expiry escalates and is never an approval. An approval never lives only in Slack: the dashboard and the HTTP endpoint keep working with the bridge stopped.

Who it is for: the person on call who gets paged for an incident and needs to answer from a phone, without opening the dashboard and typing a bearer token.

Decisions already made with the owner (2026-11-01):

| Decision | Choice |
|---|---|
| Where it runs | A separate process and compose service, not the watcher and not `phoenix-api` |
| Slack identity | A map from Slack user ID to approver name: `PHOENIX_SLACK_APPROVERS=U012ABC=alice` |
| Delivery | One configured channel |
| Message content | Summary plus a link to the dashboard incident page |

### Assumptions I am making

Correct me before the plan starts.

1. Python 3.12, `slack_bolt` (synchronous) in Socket Mode. No inbound port. The bridge only makes outbound connections.
2. The bridge reads approvals only through the API, and writes decisions only through the API. The API already redacts, so redaction stays in one place. This needs one new read endpoint, `GET /approvals/{id}` (see API changes).
3. The bridge owns exactly one database table, `approval_notifications`, through its own narrow role. It has no access to `approvals`.
4. A flat module layout would collide with `db`/`main` imports in the API tests, so the bridge is a package: `phoenix/slack/`, run as `python -m phoenix.slack.bridge`.
5. One Slack workspace and one channel. The channel is given by ID (`C0123...`), not by name.
6. The compose service is opt-in through a compose profile, so a checkout with no Slack app configured does not crash-loop.
7. A person in the channel can read the message. Only people in the ID map can answer. The evidence in the message is redacted and truncated, but it does leave the owner's network.
8. Unit tests with Slack, the API and the database faked are the CI bar. A live check against a real workspace is a manual step in the success criteria.

## Design

```
 graph --writes--> approvals (Postgres) <--reads/writes-- phoenix-api <--HTTP + bearer-- slack-bridge --Socket Mode--> Slack
                                                                                              |
                                                                                    approval_notifications
                                                                                    (bridge's own table)
```

The graph and the watcher are not modified. The graph already writes a pending row, pauses, and on resume re-reads the row and checks `decided_by in config.approvers` ([phoenix/graph/nodes.py:722](../../../phoenix/graph/nodes.py#L722)). The bridge writes `decided_by = "alice"` (the mapped name), which is in the list, so the graph recheck passes unchanged.

### The three jobs

**1. Post.** Every `PHOENIX_SLACK_POLL_SECONDS` (default 5), `GET /approvals` returns the pending requests. For each one with no row in `approval_notifications`, the bridge posts a message to the channel and records `(approval_id, channel_id, message_ts)`.

**2. Answer.** A button click arrives over Socket Mode. The bridge:

1. calls `ack()` first, inside the 3 seconds Slack allows;
2. checks the action id is one of `phoenix_approve` / `phoenix_deny` and the click came from the configured channel;
3. parses the approval id from the button value (digits only, nothing else is in the value);
4. maps `body.user.id` to an approver name. An ID that is not in the map gets an ephemeral refusal and makes no API call;
5. calls `POST /approvals/{id}/decision` with `{decision, approver, channel: "slack"}` and the bearer token;
6. turns the API's answer into an ephemeral reply to the clicker, and updates the message when the answer was recorded.

Identity comes from the signed Slack payload (`user.id`), never from message text.

**3. Settle.** Every poll, for each notification with `settled_at IS NULL`, the bridge calls `GET /approvals/{id}`. When the status is no longer `pending`, or the request is `overdue`, it edits the message to remove the buttons and show the outcome (who, which channel, when, or "expired, escalated"), then sets `settled_at`. This runs whichever channel won, so a dashboard answer also closes the Slack message. It is what satisfies "update the Slack message with who decided and when" in the decisions doc.

### The message

Block Kit, with a plain-text fallback for notifications.

- Header: `Approval needed: {action} on {container}`
- Fields: incident id and service, tier, expiry time (Slack date formatting)
- `incident_summary` (already capped at 300 characters by the graph)
- The three most recent evidence summaries (`source: summary`)
- Link: `{PHOENIX_DASHBOARD_URL}/dashboard/#/incidents/{incident_id}`
- Buttons: Approve (with Slack's `confirm` dialog) and Deny (no confirm). The button value is the approval id and nothing else.

Every dynamic string is escaped for Slack (`&`, `<`, `>`) and truncated to Slack's block limits. Evidence comes from logs and tool output, so without escaping, a log line containing `<!channel>` would ping the whole channel.

### Click replies

| API answer | What the clicker sees |
|---|---|
| 200 recorded | Message updated to the settled text |
| 409, first answer already won | "Already approved. The first answer wins, so your click changed nothing." (ephemeral). The 409 names the winning status but not who or which channel, so who and when appear on the message when the settle pass edits it |
| 409, expired | "The deadline passed. This escalates and is never an approval." (ephemeral) |
| 403, name not on the API's approver list | "Your Slack ID maps to a name that is not on the approver list. Ask the owner." (ephemeral) |
| 401 or 503 | "Approvals are not configured on the server. Use the dashboard." (ephemeral) |
| 404 | "That request no longer exists." (ephemeral) |
| API unreachable | "Could not reach Phoenix. Use the dashboard." (ephemeral) |

### Failure handling

The bridge fails closed on configuration and degrades open on Slack: Slack being down must not affect anything else.

- Invalid or missing configuration stops the process at startup with a clear error: missing token, wrong token prefix (`xapp-` / `xoxb-`), empty approver map, malformed map entry, non-ID channel. It never falls back to something looser.
- Slack unreachable or rate-limited on post: log, leave the notification unrecorded, retry on the next poll. A `Retry-After` is honored.
- Database unreachable: skip the post pass. Posting without being able to record it would repost on the next pass.
- API unreachable: no posting, no settling. Clicks get the "use the dashboard" reply.
- Socket Mode drops: Bolt reconnects. A click lost during the gap is not retried by anyone. The approver uses the dashboard.
- Crash between `chat.postMessage` and the insert: the message is posted twice on restart. Posting is at-least-once, and that is accepted. A duplicate is harmless because both buttons carry the same approval id and the first answer wins.
- At startup the bridge posts for approvals that are already pending. The TTL (default 900 seconds) bounds how old they can be.

## API changes (small, additive)

1. `ApprovalDecision` ([phoenix/api/schemas.py](../../../phoenix/api/schemas.py)) gains `channel: Literal["api", "dashboard", "slack"] = "api"`. [phoenix/api/main.py:138](../../../phoenix/api/main.py#L138) passes it to `db.decide_approval` instead of the hard-coded `"api"`. Existing callers send no `channel` and behave as before. The caller holds the bearer token, so the channel is an assertion by a trusted caller, the same as the approver name is today.
2. New `GET /approvals/{id}` returns one approval in any status, in the same redacted shape `GET /approvals` already uses (from `incident_trace._approvals`), plus `incident_id` and `service_name`. 404 when absent. It follows the other read endpoints, which are open.
3. New `db.approval_by_id` query: `PENDING_APPROVALS` without the status filter.

## Database changes

**`phoenix/db/init/009_approval_notifications.sql`**

```sql
CREATE TABLE approval_notifications (
    approval_id BIGINT PRIMARY KEY REFERENCES approvals(id),
    channel_id TEXT NOT NULL,
    message_ts TEXT NOT NULL,
    posted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    settled_at TIMESTAMPTZ
);
```

This is a separate table because the 007 migration states that the only UPDATE the application runs on `approvals` is the one transition out of `pending`, and "nothing rewrites a decided row". Slack bookkeeping must not break that.

**`phoenix/db/init/010_create_slack_bridge_role.sh`**: same shape as `006_create_payment_role.sh`. It creates `slack_bridge` with `SLACK_BRIDGE_PASSWORD` and grants it `SELECT, INSERT` on `approval_notifications` plus `UPDATE (settled_at)` only. It gets no grant on `approvals`, `incidents` or anything else. The Postgres service in compose gains `SLACK_BRIDGE_PASSWORD`.

Init scripts only run on an empty data directory. An existing lab database needs the two files applied by hand. The README documents the command.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `SLACK_APP_TOKEN` | required | App-level token, must start `xapp-` |
| `SLACK_BOT_TOKEN` | required | Bot token, must start `xoxb-` |
| `PHOENIX_SLACK_CHANNEL` | required | Channel ID the messages go to |
| `PHOENIX_SLACK_APPROVERS` | required | `U012ABC=alice,U034DEF=bob`. Names must also be in `PHOENIX_APPROVERS` |
| `PHOENIX_APPROVAL_TOKEN` | required | The same bearer token the API checks |
| `PHOENIX_API_URL` | `http://phoenix-api:8000` | Where the bridge reaches the API |
| `PHOENIX_DASHBOARD_URL` | required | Base URL a person's browser can open, for the link |
| `PHOENIX_SLACK_POLL_SECONDS` | `5` | Post and settle interval |
| `DATABASE_URL` | required | The `slack_bridge` role's connection |
| `SLACK_BRIDGE_PASSWORD` | required (compose) | Passed to Postgres to create the role |

Slack app setup the README must describe: Socket Mode on; an app-level token with `connections:write`; Interactivity on (no request URL needed in Socket Mode); bot scope `chat:write`; the bot invited to the channel.

The bridge cannot read the API's `PHOENIX_APPROVERS`. A mapped name that is not on that list shows up as a 403 reply on the first click, and the bridge logs it. A startup cross-check is not possible without a new endpoint and is out of scope.

## Tech stack

- Python 3.12 (the API image's base)
- `slack_bolt` and `slack_sdk` (Socket Mode)
- `httpx` for API calls (or `requests`, whichever the plan finds already pinned; the API image has neither)
- `psycopg[binary,pool]==3.2.3` (as in `phoenix/api/requirements.txt`)
- Exact pins for the new packages are chosen in the plan and listed for approval. Adding dependencies is an "ask first" item.

## Commands

```
Test the bridge:      python -m pytest phoenix/slack -q
Test the API change:  python -m pytest phoenix/api -q
Full suite:           python -m pytest -q
Run locally:          python -m phoenix.slack.bridge
Run in compose:       docker compose --profile slack up -d --build slack-bridge
Bridge logs:          docker compose logs -f slack-bridge
Apply to an old DB:   docker compose exec -T postgres psql -U phoenix -d phoenix < phoenix/db/init/009_approval_notifications.sql
```

The exact test invocations are confirmed against the repo's current setup in the plan. The repo has no `pytest.ini`, and the API tests import `db` and `main` flat.

## Project structure

```
phoenix/slack/__init__.py
phoenix/slack/config.py        Env parsing. Raises SlackConfigError at startup, in the style of safety_policy.PolicyConfigError
phoenix/slack/messages.py      Pure functions: Block Kit for pending, settled and refusal text. Escaping and truncation
phoenix/slack/api_client.py    The three API calls and the mapping of their answers
phoenix/slack/store.py         approval_notifications SQL (record, list unsettled, settle)
phoenix/slack/handlers.py      Click logic as a function of (slack user, action, approval id), with ack done by the caller
phoenix/slack/bridge.py        Entry point: config, Bolt app, Socket Mode connect, the poll loop
phoenix/slack/requirements.txt
phoenix/slack/Dockerfile       Build context ./phoenix, so the package layout is kept
phoenix/slack/test_*.py
phoenix/db/init/009_approval_notifications.sql
phoenix/db/init/010_create_slack_bridge_role.sh
```

Also touched: `phoenix/api/{schemas,main,db}.py` and their tests, `docker-compose.yml`, `.env.example`, `phoenix/test_compose_env.py`, `README.md`.

## Code style

Match the surrounding code: a module docstring that says why, not what; comments only where the reason is not obvious; narrow exception handling with `# noqa: BLE001 - reason` where a broad catch is deliberate; fail-closed errors as a named exception.

```python
class SlackConfigError(RuntimeError):
    """The bridge's configuration is unusable. The process stops instead of guessing."""


def slack_approvers(env=os.environ) -> dict[str, str]:
    """Slack user id -> approver name, from PHOENIX_SLACK_APPROVERS.

    An empty map is an error, not "nobody may answer": a bridge that posts buttons
    no one can use would look healthy and do nothing.
    """
    raw = env.get(ENV_SLACK_APPROVERS, "")
    mapping: dict[str, str] = {}
    for entry in filter(None, (part.strip() for part in raw.split(","))):
        user_id, sep, name = entry.partition("=")
        if not sep or not user_id.strip() or not name.strip():
            raise SlackConfigError(f"{ENV_SLACK_APPROVERS} entry {entry!r} must look like U012ABC=alice")
        mapping[user_id.strip()] = name.strip()
    if not mapping:
        raise SlackConfigError(f"{ENV_SLACK_APPROVERS} is empty, so no one could answer from Slack")
    return mapping
```

Tests are plain pytest functions named for the behavior, as in [phoenix/api/test_approval_endpoint.py](../../../phoenix/api/test_approval_endpoint.py).

## Testing strategy

Tests first, per the decisions doc. Slack, the API and the database are faked. No test needs a network, Docker or a Slack workspace.

| Level | What it proves |
|---|---|
| `messages` | Escapes `& < >` (a `<!channel>` in evidence arrives inert). Truncates to block limits. The link is built from the incident id. The Approve button has a confirm dialog and the Deny button does not. Button values hold only the approval id. Settled text names who, channel and time |
| `config` | Every bad value fails at startup: missing tokens, wrong prefixes, empty or malformed map, bad channel id, missing URL |
| `handlers` | Unmapped Slack user makes no API call. A click from another channel or with another action id is ignored. A non-numeric value is ignored. Each API answer in the table above gives its reply. A timeout gives the fallback reply |
| `bridge` loops | A pending approval is posted once, and not again after a restart. A dashboard-decided approval settles the Slack message. An overdue one settles as expired. A DB failure skips posting. A Slack error is retried on the next pass. A decided approval already settled is not edited again |
| API | `channel` is accepted, defaulted to `api`, rejected when outside the Literal, and recorded. `GET /approvals/{id}` returns the redacted shape, 404s, and redacts planted secrets. Existing endpoint tests still pass unchanged |
| SQL text | The migration grants the role nothing on `approvals`. `UPDATE` is limited to `settled_at`, checked the way `test_approval_endpoint` checks the decision statement |
| Compose | `.env.example` names the new variables with placeholders only. The `slack-bridge` service sits behind a profile, receives its settings from the host environment, and publishes no ports |
| Lint | `phoenix/test_modules_define_what_they_use.py` covers the new test files automatically |

Manual, once, before the work is called done: run the lab in `guarded` mode, trigger the bad-deploy scenario, approve from Slack, and confirm the run resumes and the trace shows `approved ... via slack`. Repeat with the dashboard answering first, and with the request left to expire.

## Boundaries

**Always**
- Write tests before the code they cover.
- Escape every dynamic string that goes into a Slack message.
- Make the API the only writer of `approvals`, and the only place that checks the token, the approver list and expiry.
- Fail closed on configuration at startup.
- Keep the new code in the style of the surrounding files.

**Ask first**
- Adding `slack_bolt`, `slack_sdk`, `httpx` or any other dependency, and the exact versions.
- The `009` and `010` migrations, and the new `slack_bridge` role.
- The new `GET /approvals/{id}` endpoint and the `channel` field on the decision body.
- The compose service, its profile, and any change to the Postgres service environment.
- Any change to `approve.js` or the dashboard (see open question 1).
- Committing anything. Commits are authored only by the owner, with no Claude or co-author attribution lines.

**Never**
- Change the graph, the watcher or `safety_policy`.
- Write to `approvals` from the bridge, or give its role any grant on that table.
- Take an identity, an action, a container or any approval detail from the Slack payload text. Only `user.id`, the action id and the approval id are read from it.
- Put a token, a button secret or unredacted evidence in a message or a log line.
- Open an inbound port or use HTTP mode.
- Auto-approve, or treat expiry or a lost click as a decision.
- Post resolved or escalated outcomes, DMs, reminders, slash commands or modals (see Not doing).

## Success criteria

1. A pending Tier 2 approval appears in the configured channel within two poll intervals, showing the action, target, tier, one-line diagnosis, up to three evidence lines, the expiry, and a working dashboard link.
2. Approving from Slack as a mapped user records the row as `approved` with `decided_by` set to the mapped name and `decision_channel = 'slack'`. The watcher resumes the run and the trace reads "alice approved ... via slack".
3. A Slack user who is not in the map gets an ephemeral refusal. No API call is made and the row stays `pending`.
4. A second click, or a click after another channel answered, changes nothing and tells the clicker who won.
5. If the dashboard answers first, the Slack message is edited within two poll intervals to show who, which channel and when, with the buttons removed.
6. An unanswered request ends with the buttons removed and the text saying it expired and escalates. No code path approves it.
7. With the bridge stopped, or Slack unreachable, dashboard and HTTP approvals behave exactly as they do today.
8. Restarting the bridge does not repost a request that already has a message.
9. A planted `<!channel>` and a planted secret in evidence reach Slack inert and redacted respectively.
10. The bridge refuses to start, with a message naming the variable, for each invalid configuration listed under Testing.
11. The existing test suite passes unchanged, and the new tests pass.
12. At startup the bridge checks that `approval_notifications` is readable with its role. If not, it exits at once with a message naming migrations `009` and `010` and the README section. It never starts half-working.
13. The README no longer lists Slack buttons as not built, documents the Slack app setup and the old-database migration step, and the stale line saying compose does not pass `PHOENIX_APPROVERS` is corrected ([docker-compose.yml:233](../../../docker-compose.yml#L233) passes it now).

## Not doing, and why

- **Outcome posts** (resolved, escalated, action taken): a notification feature, not an approval one. The same bridge can add it later.
- **DMs to each approver**: N messages to settle for one decision. One channel proves the path.
- **Reminders, "deny with reason" modals, slash commands**: not needed to prove the path, and a second way to approve is a second surface to secure.
- **HTTP mode or any inbound port**: ruled out by the Socket Mode decision.
- **Slack columns on `approvals`**: breaks the 007 rule that decided rows are never rewritten.
- **Slack OAuth distribution or multi-workspace**: this is a self-hosted tool with one Slack app the owner creates.
- **Looking up Slack emails**: needs `users:read.email` and a call per click. The explicit ID map avoids both.
- **A startup cross-check of the map against `PHOENIX_APPROVERS`**: the bridge cannot read the API's list.

## Resolved decisions

All six questions were answered by the owner on 2026-11-01, taking the recommended default each time.

1. **Dashboard channel label:** left alone. The dashboard keeps recording `via api`. A follow-up can make it send `channel: "dashboard"`.
2. **How the bridge reads approvals:** through the new `GET /approvals/{id}`, so redaction and every approvals read stay in the API and the bridge role has no grant on `approvals`.
3. **Already-pending requests at startup:** the bridge posts them. The TTL bounds their age.
4. **Channel visibility:** accepted. The README says to use a private, approvers-only channel. The bridge does not enforce it.
5. **Dependency pins:** proposed in the plan and approved by the owner before anything is installed.
6. **Existing lab databases:** manual setup. The README documents the exact commands to apply `009` and `010` in order after adding `SLACK_BRIDGE_PASSWORD` and recreating the Postgres container. No migration runner. The startup check in success criterion 12 makes a missed step obvious.
