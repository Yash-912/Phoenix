# Phoenix

Phoenix is an agentic reliability engineer for a small distributed application. It is given an objective, resolve the incident and restore the SLO, rather than a runbook. It investigates telemetry, deployment history and source code, tests competing root-cause hypotheses, and picks the least invasive remediation the evidence supports.

The remediation tiers, from least to most permanent:

| Tier | Action | Examples |
|---|---|---|
| 1. Mitigate | Act on a running container | restart, pause or resume a worker, clear one cache key |
| 2. Recover | Reverse a specific change | roll back a deployment, roll back a config value |
| 3. Fix | Change the code | generate a scoped patch, run tests, open a pull request (a human merges) |

It can also decide not to act. When no hypothesis has two independent sources behind it, a run ends in `insufficient_evidence` and escalates, and no remediation tool is reached.

The product requirements are in [phoenix-prd.md](phoenix-prd.md) and the build sequence is in [phoenix-implementation-plan.md](phoenix-implementation-plan.md).

## Status

This is a lab project under active development, not a production system.

- Phases 0 to 6 are complete: the lab, deterministic chaos injection, investigation, Tier 1, Tier 2, Tier 3, and the escalation path. All five scenarios have been run live.
- Not built yet: the execution-policy model (Phase 7), incident memory (Phase 8) and the dashboard (Phase 9).
- Remediation is deliberately narrow. Rollback only covers `checkout-service` (`v17` and `v18`), and config rollback only covers `auth-service` `DB_POOL_SIZE`. The tools re-check every argument against allowlists, so the model can never supply an arbitrary service, version or command.

## How it works

```
Alertmanager --> phoenix-api --> incidents table (Postgres)
                                        |
                          watcher polls for uninvestigated incidents
                                        v
   observer --> diagnoser --> router --> remediator --> verifier
   (tools)      (scoring)       |                          |
                                +--> Tier 3 subgraph       +--> resolved / re-investigate
                                +--> escalated / insufficient_evidence
```

- **Observer** calls read-only tools: Prometheus metrics, Loki logs, container health, deployment history, git.
- **Diagnoser** scores hypotheses by how many independent evidence sources support them, instead of asking the model to guess.
- **Router** applies the confidence threshold, the token budget and the iteration cap.
- **Verifier** checks recovery against live signals, not against the service's own `/health` claim.
- Every tool call and decision is written to an append-only `audit_log`. The database role the agent uses has `SELECT` and `INSERT` only on that table.

## Repository layout

| Path | Contents |
|---|---|
| `phoenix/api/` | FastAPI service that receives Alertmanager webhooks and stores incidents |
| `phoenix/graph/` | The LangGraph agent: nodes, scoring, router, verification, persistence, watcher |
| `phoenix/tools/` | Tools the agent can call (metrics, logs, docker, git, patch, remediation, GitHub PR) |
| `phoenix/db/init/` | Postgres schema and role setup |
| `services/` | The application under investigation: gateway, auth, checkout, payment, worker |
| `chaos/` | One-command failure injection for each scenario, plus reset |
| `observability/` | Prometheus, Alertmanager, Loki and Promtail config |

## The five scenarios

| # | Scenario | Injection | Expected outcome |
|---|---|---|---|
| 1 | Bad deployment | `python -m chaos.deploy_bad_v18` | Tier 2 rollback to `v17` |
| 2 | Slow query | `python -m chaos.slow_query` | Tier 3 patch and pull request |
| 3 | Memory leak | `python -m chaos.memory_leak` | Tier 1 restart, then a Tier 3 fix |
| 4 | Config regression | `python -m chaos.config_pool` | Tier 2 config rollback |
| 5 | Ambiguous blip | `python -m chaos.ambiguous` | `insufficient_evidence`, escalated |

Each script accepts `--reset`. `python -m chaos.reset_all` returns the whole lab to healthy.

## Running it

Requirements: Docker with Compose, Python 3.12, an OpenAI-compatible LLM endpoint, and the authenticated `gh` CLI if you want Tier 3 to open real pull requests.

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

   or start the watcher, which investigates new incidents as Alertmanager creates them:

   ```
   python -m phoenix.graph.watcher
   ```

Service ports on the host: gateway 8005, checkout 8001, auth 8002, payment 8003, worker 8004, Phoenix API 5000, Prometheus 9091, Alertmanager 9093, Loki 3100.

## Tests

```
pip install pytest
pytest
```

Most tests are unit tests. The `test_live_*` modules and `phoenix/test_tier3_e2e.py` need the lab running, and separating them from the unit tests is on the to-do list.

## Known limitations

This is a lab, and it is not safe to expose as is:

- The webhook and chaos endpoints are unauthenticated, and `CHAOS_ENABLED=true` is set in `docker-compose.yml`.
- Compose publishes most service ports on all interfaces, and some containers mount the Docker socket.
- The agent runs on the host rather than in a container, and rollback shells out to `docker compose` on the same machine.
- Service URLs for verification and health checks are hardcoded to `localhost`.

## License

[MIT](LICENSE)
