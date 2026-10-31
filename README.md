# Phoenix

Phoenix is an agentic reliability engineer for a small distributed application. It is given an objective, resolve the incident and restore the SLO, rather than a runbook. It investigates telemetry, deployment history and source code, tests competing root-cause hypotheses, and picks the least invasive remediation the evidence supports.

The remediation tiers, from least to most permanent:

| Tier | Action | Examples |
|---|---|---|
| 1. Mitigate | Act on a running container | restart a service (pause, resume and clear-cache tools exist but are not routed yet) |
| 2. Recover | Reverse a specific change | roll back a deployment, roll back a config value |
| 3. Fix | Change the code | generate a scoped patch, run tests, open a pull request (a human merges) |

It can also decide not to act. When no hypothesis has two independent sources behind it, a run ends in `insufficient_evidence` and escalates, and no remediation tool is reached.

The product requirements are in [phoenix-prd.md](phoenix-prd.md) and the build sequence is in [phoenix-implementation-plan.md](phoenix-implementation-plan.md).

## Status

This is a lab project under active development, not a production system.

Built:

- **Phases 0 to 6**: the lab, deterministic chaos injection, investigation, Tier 1, Tier 2, Tier 3, and the escalation path. All five scenarios have been run live.
- **Phase 7, the execution policy**: two modes, `guarded` (the default) and `autonomous_lab`. In `guarded` mode a Tier 2 action stops and waits for a person to approve it. The run pauses, and an approve or deny call resumes it. An unanswered request expires into an escalation, never an approval. See [Execution policy and approvals](#execution-policy-and-approvals).
- **Phase 8, incident memory**: when an incident reaches a terminal status, a compact record is written to `incident_memory`. A later incident on the same service with the same root-cause category gets a "matches incident #X" note in the audit trail. The note is written after the run finishes, so it never influences scoring or the action taken.
- **Offline replay** (`python -m phoenix.graph.replay`): re-scores stored incidents with the current evidence logic, without calling a model.

Not built yet:

- The dashboard (Phase 9). Today the only views are the API (`/incidents`, `/docs`) and the `audit_log` table.
- Slack approval buttons. Approvals are answered through the HTTP endpoint only.
- Redaction of tool output before it reaches the model.
- The evaluation runner and the stability gate (10 live runs per scenario). Scenarios have been run live, but there is no saved result file with a pass rate.
- Alert grouping and per-service locks. The watcher investigates one incident at a time.
- Traces. The lab exports metrics (Prometheus) and logs (Loki) only.

Remediation is deliberately narrow. Rollback only covers `checkout-service` (`v17` and `v18`), and config rollback only covers `auth-service` `DB_POOL_SIZE`. The tools re-check every argument against allowlists, so the model can never supply an arbitrary service, version or command.

## How it works

```
Alertmanager --> phoenix-api --> incidents table (Postgres)
                                        |
                          watcher polls for uninvestigated incidents
                                        v
   observer --> diagnoser --> router --> remediator --> verifier
   (tools)      (scoring)       |            |             |
                                |            |             +--> resolved / re-investigate
                                |            +--> approval gate (Tier 2, guarded mode)
                                +--> Tier 3 subgraph (investigate, patch, validate, open PR)
                                +--> escalated / insufficient_evidence
```

- **Observer** calls read-only tools: Prometheus metrics, Loki logs, container health, deployment history, git.
- **Diagnoser** scores hypotheses by how many independent evidence sources support them, instead of asking the model to guess. The confidence threshold is 0.75, and a run is capped at 5 iterations and a token budget.
- **Router** applies the confidence threshold, the token budget and the iteration cap.
- **Remediator** maps the root-cause category to an action through a fixed table in `phoenix/graph/remediation_policy.py`, re-reads the live state before acting, and asks for approval when the execution policy requires it.
- **Verifier** checks recovery against live signals, not against the service's own `/health` claim.
- **Tier 3** investigates the service's code, generates a scoped patch in a git worktree, runs the tests with a scrubbed environment, and opens a pull request through `gh`. The only GitHub operations it can perform are opening a pull request and commenting on one. There is no merge or approve function.
- Every tool call and decision is written to an append-only `audit_log`. The database role the agent uses has `SELECT` and `INSERT` only on that table, and on `incident_memory`.

The model proposes a hypothesis and names its category. Code decides the threshold, the action, its target, whether approval is needed, execution and verification.

## Execution policy and approvals

Configuration is read from environment variables when the watcher and the API start. An invalid value stops the process instead of falling back to something looser.

| Variable | Default | Meaning |
|---|---|---|
| `PHOENIX_EXECUTION_POLICY` | `guarded` | `guarded` or `autonomous_lab`. In `autonomous_lab` nothing waits for approval. |
| `PHOENIX_TIER1_REQUIRES_APPROVAL` | `false` | Set `true` to hold Tier 1 actions for approval too. |
| `PHOENIX_TIER2_REQUIRES_APPROVAL` | unset | Tier 2 always requires approval in `guarded` mode. Setting this to `false` is refused at startup. |
| `PHOENIX_APPROVAL_TTL_SECONDS` | `900` | How long a request stays open before it expires into an escalation. |
| `PHOENIX_APPROVERS` | empty | Comma-separated identities allowed to answer. Read by the watcher and the API. |
| `PHOENIX_APPROVAL_TOKEN` | unset | Bearer token the API requires on `/approvals/{id}/decision`. With no token the endpoint refuses everything (503). |
| `PHOENIX_WATCH_INTERVAL_SECONDS` | `10` | Watcher poll interval. |

Tier 3 never reaches an approval gate. It stops at a pull request, and a human merges.

To answer a pending request:

```
curl -X POST http://localhost:5000/approvals/<id>/decision \
  -H "Authorization: Bearer $PHOENIX_APPROVAL_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"decision": "approve", "approver": "<name on the approver list>"}'
```

The first answer wins and a later one is ignored. An answer after the deadline is refused. The watcher resumes the paused run, and the remediator re-reads the real system state before it acts.

## Repository layout

| Path | Contents |
|---|---|
| `phoenix/api/` | FastAPI service: receives Alertmanager webhooks, lists incidents, records approval decisions |
| `phoenix/graph/` | The LangGraph agent: nodes, scoring, router, verification, safety policy, approvals, incident memory, replay, persistence, watcher |
| `phoenix/tools/` | Tools the agent can call (metrics, logs, docker, git, repo search, patch, worktree, remediation, GitHub PR) |
| `phoenix/db/init/` | Postgres schema, roles and migrations `001` to `008` |
| `services/` | The application under investigation: gateway, auth, checkout, payment, worker |
| `chaos/` | One-command failure injection for each scenario, plus reset |
| `observability/` | Prometheus, Alertmanager, Loki and Promtail config |
| `infra/docker-socket-proxy/` | The HAProxy allowlist for the Docker socket proxy |
| `docs/` | Design decisions and earlier phase plans |

## The five scenarios

| # | Scenario | Injection | Expected outcome |
|---|---|---|---|
| 1 | Bad deployment | `python -m chaos.deploy_bad_v18` | Tier 2 rollback of `checkout-service` to `v17` |
| 2 | Slow query | `python -m chaos.slow_query` | Tier 3 patch and pull request (`payment-service`) |
| 3 | Memory leak | `python -m chaos.memory_leak` | Tier 1 restart, then a Tier 3 fix (`worker-service`) |
| 4 | Config regression | `python -m chaos.config_pool` | Tier 2 config rollback (`auth-service`) |
| 5 | Ambiguous blip | `python -m chaos.ambiguous` | `insufficient_evidence`, escalated |

Each script accepts `--reset`. `python -m chaos.reset_all` returns the whole lab to healthy.

In `guarded` mode, scenarios 1 and 4 pause for approval before the rollback. In `autonomous_lab` mode they roll back without waiting.

## Running it

Requirements: Docker with Compose, Python 3.11 or newer (the suite was last run on 3.11), an OpenAI-compatible LLM endpoint, and the authenticated `gh` CLI if you want Tier 3 to open real pull requests.

1. Create `.env` from the template and set strong passwords:

   ```
   cp .env.example .env
   ```

   Add the LLM settings the agent reads:

   ```
   LLM_BASE_URL=...
   LLM_API_KEY=...
   LLM_MODEL=...
   ```

   Optionally add the execution-policy variables from the table above. The agent and the watcher read them from the environment they run in.

2. Start the lab:

   ```
   docker compose up -d --build
   ```

3. Install the agent's dependencies in a virtual environment:

   ```
   pip install -r phoenix/graph/requirements.txt
   ```

4. The agent runs on the host and reaches the lab through the ports compose publishes. Export `DATABASE_URL` for the `phoenix_app` role (the same value `phoenix-api` gets in compose, with host `localhost`), then run one investigation by hand:

   ```
   python -m phoenix.graph.graph <incident_id> <service_name>
   ```

   or start the watcher, which investigates new incidents as Alertmanager creates them, resumes runs whose approval has been answered, and expires overdue requests:

   ```
   python -m phoenix.graph.watcher
   ```

   The watcher ignores incidents that existed when it started.

Service ports on the host: gateway 8005, checkout 8001, auth 8002, payment 8003, worker 8004, Phoenix API 5000, Prometheus 9091, Alertmanager 9093, Loki 3100, cAdvisor 8080, Postgres 5432, Redis 6379, RabbitMQ 5672 (management UI 15672), Docker socket proxy 2375 (bound to `127.0.0.1`).

The Docker socket proxy only allows `GET /containers/{name}/json` and `POST` restart, pause and unpause. It does not allow create or exec. Its image is pinned by digest because the rule order lives inside the image.

## Tests

```
pip install pytest
pytest
```

`pytest` collects 1114 tests. The last full run of the non-live tests (2026-10-21) had 1108 passing and 0 failing. The `test_live_*` modules and `phoenix/test_tier3_e2e.py` need the lab running, and separating them from the unit tests is on the to-do list.

## Known limitations

This is a lab, and it is not safe to expose as is:

- The webhook and chaos endpoints are unauthenticated, and `CHAOS_ENABLED=true` is set in `docker-compose.yml`.
- Approvals are protected by one shared bearer token, and the approver name is whatever the caller asserts. There is no per-user identity (OIDC) yet.
- `docker-compose.yml` does not pass `PHOENIX_APPROVERS` or `PHOENIX_APPROVAL_TOKEN` to the `phoenix-api` container, so the approval endpoint answers 503 until you add them to that service's environment.
- A run waiting for approval lives in the watcher's memory. If the watcher restarts, the run is dropped and its request expires into an escalation.
- Compose publishes most service ports on all interfaces, and the Docker socket is mounted read-only into the socket proxy, cAdvisor and Promtail.
- The agent runs on the host rather than in a container, and rollback shells out to `docker compose` on the same machine.
- Service URLs for verification and health checks default to `localhost`.
- The watcher runs one investigation at a time.

## License

[MIT](LICENSE)
