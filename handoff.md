# Project Handoff

Written 2026-10-21 by session agentic-3c for whoever continues. At the user's request this file alone was committed to `main`; the proxy fix (`6a3e741`) is only on `integration/phase7-8`. Nothing here contains secret values.

## 1. Objective

- Original request (from session agentic-ec): verify everything built so far in Phoenix, from unit tests through live scenarios, then report what passed, what failed and what was not run.
- Later user steering, in order: run the remaining live scenarios; explain and fix the docker socket proxy 403 found by scenario 3 (test the fix first, apply, commit and push only if it works); fix two stale comments; rerun scenario 3; run the full suite; prepare this handoff.
- Done means: results reported (done), proxy fix applied and pushed (done), comment fixes made (done on disk, not committed), scenario 3 rerun and full suite run (done).
- Not requested: merging, opening PRs, editing anything else. The user did not authorize committing the two comment fixes.

## 2. Project / System Context

- Phoenix is an incident-response agent. It watches Alertmanager alerts and diagnoses them with an LLM using a LangGraph graph. It acts in tiers: Tier 1 restart or pause, Tier 2 rollback of a deployment or config, Tier 3 code patch and PR. An approval gate and safety policy sit in front of actions (`guarded` is the default, `autonomous_lab` skips approvals), and incident memory records terminal outcomes.
- Python 3.11 venv at `D:\Agentic\agentic`. Postgres holds incidents, approvals, audit_log, evidence, hypotheses, incident_memory and charges. The lab is a 15-service docker compose project named `integ-phase7-8`: alertmanager, api-gateway, auth-service, cadvisor, checkout-service, docker-socket-proxy, loki, payment-service, phoenix-api, postgres, prometheus, promtail, rabbitmq, redis, worker-service.
- The LLM is a local Curvet proxy at `http://127.0.0.1:4141/v1`, started by the user's terminal (`curvet proxy`). phoenix-api answers on `http://127.0.0.1:5000/docs`.
- GitHub remote: `https://github.com/Yash-912/Phoenix.git`.

## 3. Current State

- Working (observed this session): the full non-live suite (1108 passed); Tier 2 rollback of a bad deploy (scenario 1) and a bad config (scenario 4) in `autonomous_lab`; guarded mode pausing for approval on both; the ambiguous fault ending `insufficient_evidence` (scenario 5) in both modes; Tier 3 patch and PR (scenario 2); Tier 1 `restart_service` through the socket proxy after the fix (scenario 3).
- Broken before the fix: Tier 1 restart and pause always got 403 from the proxy. Fixed in `6a3e741` (section 11).
- Not changed or not observed: scenario 2 in `guarded` mode; Gate 0 (10 live runs per scenario); `phoenix/test_tier3_e2e.py` (opt-in); verifier findings F4–F15 (copied from the earlier handoff, not rechecked by me).
- Last full suite: 1108 passed, 0 failed, 13m46s, run on `6a3e741` plus the `docker_tool.py` docstring edit. The `deployer.py` docstring edit landed while it was running. Both edits are docstring-only; no test run since.

## 4. Changes Made During This Session

Committed as `6a3e741` ("allow restart, pause and unpause through the socket proxy with an explicit haproxy allowlist"), pushed to `origin/integration/phase7-8`:
- `infra/docker-socket-proxy/haproxy.cfg.template` (new): stock template header and backends, then an explicit frontend allowlist: GET `/containers/{name}/json`, POST `restart|pause|unpause`, GET `_ping|version`, then `http-request deny`, then `default_backend dockerbackend`.
- `docker-compose.yml`: the `docker-socket-proxy` service now pins the image by digest (`@sha256:1f5038b5...3459`), drops all `environment:` flags and mounts the template read-only.
- `phoenix/test_docker_socket_proxy_compose.py` (rewritten): evaluates the template's allow rules against requests and checks the compose wiring and digest pin. The old test asserted env flags, which is why unit tests passed while live was broken.
- `.gitattributes`: added `infra/docker-socket-proxy/*.template text eol=lf`.

Uncommitted, docstring only:
- `phoenix/tools/docker_tool.py::get_container_state` docstring: replaced the `CONTAINERS=1` / GET-only claim with the real allowlist.
- `chaos/lib/deployer.py` module docstring: replaced the `GET/POST /containers/*` claim.

## 5. Files Touched

- Created: `infra/docker-socket-proxy/haproxy.cfg.template`.
- Modified (committed): `docker-compose.yml`, `phoenix/test_docker_socket_proxy_compose.py`, `.gitattributes`.
- Modified (uncommitted): `phoenix/tools/docker_tool.py`, `chaos/lib/deployer.py`.
- Untracked, not mine: `docs/live-verification.md`, `docs/status-diagnosis.md`, `docs/tier3-live.md`, `docs/tier3-rerun.md`, and this file. In the main checkout `D:\Agentic`, `phoenix-implementation-plan.md` is modified and `docs/phoenix-v1-decisions.md`, `novelty_thoughts.md` are untracked. These are the user's; do not commit them.
- Investigated, unchanged: `phoenix/db/init/001`–`008`, `phoenix/tools/remediation_tool.py`, `chaos/slow_query.py`.
- Removed on first attempt: `deployments/docker-socket-proxy/` (it is gitignored, so the template could not live there).

## 6. Important Code Locations

- `phoenix/tools/remediation_tool.py::ALLOWED_DOCKER_ACTIONS` — the three Tier 1 verbs and their proxy paths.
- `phoenix/tools/docker_tool.py::get_container_state` — the only GET through the proxy.
- `chaos/lib/deployer.py` — Tier 2 rollback; shells out to compose instead of using the proxy.
- `phoenix/graph/watcher.py` — `python -m phoenix.graph.watcher`, the polling entry point.
- `phoenix/graph/graph.py`, `nodes.py`, `tier3_nodes.py` — graph, remediator and Tier 3 path (`push_branch`, `pr_opener_node`).
- `phoenix/graph/safety_policy.py`, `approvals.py` — policy and approval gate.
- `phoenix/graph/memory.py` — incident memory (`incident_memory` table).
- `chaos/{deploy_bad_v18,slow_query,memory_leak,config_pool,ambiguous,reset_all}.py` — scenario injectors and reset.

## 7. Decisions and Reasoning

- Proxy: a custom allowlist template, not `POST=1`. The stock template runs `http-request deny unless METH_GET || env(POST)` before the `ALLOW_*` rules, so those flags do nothing with `POST=0`. With `POST=1`, the generic `/containers` rule would also allow create and exec (host-root equivalent). Alternatives not tried: an older proxy image, a different proxy.
- Image pinned by digest because the rule order lives inside the image.
- Template moved to `infra/` because `deployments/` is gitignored.
- Kept the cleanup rule from the first handoff: close and delete only the PR branch a run created. The comment cleanup went beyond that rule and was not done.
- Branch pushed: `integration/phase7-8` only. `main` was not touched.
- User preference (saved in memory): no Claude co-author or attribution lines in commits; commits must look human-written and be authored only by the user.

## 8. What We Tried

### Worked
- Throwaway proxy on port 2376 with the custom template: 30/30 allow/deny checks passed, including real restart and pause/unpause on `worker-service`. Then 22/22 on the real proxy at 2375, then `remediation_tool.restart_service("worker-service")` returned `ok`.
- Throwaway database for migrations: `001`, `002`, `004`, `005`, `007`, `008` applied with `ON_ERROR_STOP`; column schema identical to live (56 rows). Database dropped afterwards.

### Failed / Rejected
- First template had no `default_backend`, so allowed requests got 503. Fixed by appending `default_backend dockerbackend`.
- First scenario launch used a relative path after `source env.sh` (which does `cd`), so nothing ran. Use absolute paths.
- Deleting the leftover branch and comment was blocked by the permission classifier (section 11). Not retried.
- Rejected: `POST=1` (opens create and exec).

## 9. Commands Run

All from `D:\Agentic\.claude\worktrees\integ-phase7-8` unless noted.
- Full suite: `python -m pytest phoenix chaos -q --ignore=phoenix/test_tier3_e2e.py --ignore-glob="*test_live_*" -p no:cacheprovider` — 1108 passed. A first run of `phoenix` only gave 1057 passed (1059 collected, 2 excluded).
- Scenario driver (scratchpad): `bash -c "source <scratchpad>/env.sh; <scratchpad>/run_scenario.sh <label> <guarded|autonomous_lab> <chaos module> <timeout_s> '' [traffic_url]"` — injects, runs the watcher, dumps incident/approval/audit rows, then resets. Needs `PHOENIX_COMPOSE_PROJECT=integ-phase7-8`. The scratchpad `env.sh` sources `.env` and builds `DATABASE_URL` from `PHOENIX_APP_PASSWORD`; no secrets are in the script.
- Proxy apply: `docker compose -p integ-phase7-8 up -d --no-deps docker-socket-proxy` (recreated that one container).
- Git: `git add` of the four files, `git commit`, `git push origin integration/phase7-8` (84aaa40..6a3e741).
- `gh pr close 37 --repo Yash-912/Phoenix --delete-branch` (after scenario 2).
- Mistake: `python -m phoenix.graph.watcher --help` runs the module; it failed on missing `LLM_BASE_URL` before starting anything.

## 10. Tests and Verification

| Run | Result |
|---|---|
| Full non-live suite (phoenix + chaos) | 1108 passed, 0 failed |
| Proxy-related subset after the fix | 101 passed |
| Migrations on throwaway DB | schema identical to live; `.sh` role scripts 003/006 not exercised |
| Scenario 1 guarded / autonomous_lab | paused, no action / rolled back to v17, resolved (incidents 10, 11) |
| Scenario 4 guarded / autonomous_lab | paused, no action / pool restored to 10, resolved (12, 13) |
| Scenario 5 guarded / autonomous_lab | `insufficient_evidence`, no action (14, 15) |
| Scenario 2 autonomous_lab | patched `_find_charge_slow`, PR #37 opened, closed with branch deleted (16) |
| Scenario 3 autonomous_lab before fix | restart got 403, escalated (18) |
| Scenario 3 autonomous_lab after fix | restart completed, `memory_leak` check failed, no repeat restart, Tier 3 found PR #25 already carries the fix, `pr_exists` (19) |

- Unverified: scenario 2 in guarded mode; Gate 0; `test_tier3_e2e.py`; fresh-cluster apply of `003`/`006`; the two docstring edits (no run since, but they are comments).
- Docs freshness: the four `docs/` reports were written before these runs; see section 11.

## 11. Errors / Bugs / Blockers

- Confirmed bug, fixed: proxy 403 on restart/pause (see sections 4 and 7). Remaining risk: pinned digest means a deliberate image update must re-run `phoenix/test_docker_socket_proxy_compose.py` plus a live matrix.
- Blocker (external): deleting the run-created GitHub leftovers was denied by the permission classifier. Leftovers: remote branch `phoenix/tier3-incident-19-worker-service-1792482512`, and a comment on PR #25 ("Phoenix produced this same fix again for incident #19", posted 2026-10-20T07:48:49Z, GraphQL id `IC_kwDOTb4qKc8AAAABdq4qcQ`). PR #25 (head `phoenix/tier3-incident-71-worker-service-1791210076`) and its two older comments are preexisting. Do not delete them without the user's go-ahead.
- Suspected / unverified:
  - `incidents.status` follows the alert state, not the outcome. Incidents 18 and 19 show `active` with outcomes `escalated` and `pr_exists`.
  - Incident 17 (a second HighLatency incident during scenario 2) was never investigated and has no `incident_memory` row.
  - `chaos/reset_all` exits 0 even when a reset fails; check container state directly.
  - Verifier findings F4–F15 from the earlier handoff are still open as far as I know.
  - Possibly stale text: `phoenix/tools/remediation_tool.py` header ("The proxy was left untouched on purpose"), `docs/superpowers/specs/2026-09-30-phase3-tier1-mitigation-design.md` ("CONTAINERS=1, POST=1"), `docs/live-verification.md` (lists scenario 3 autonomous as not run).
  - The earlier handoff said incident 9 was `active`; it is now alert-resolved with outcome `pr_opened`. `docs/verification-report.md` named there does not exist.

## 12. Environment / Configuration

- Required config names (values live in `D:\Agentic\.env`, never print): `POSTGRES_PASSWORD`, `PHOENIX_APP_PASSWORD`, `PAYMENT_APP_PASSWORD`, `CURVET_APP_KEY`, `CURVET_APP_SECRET`, `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`. Runtime: `DATABASE_URL` (as `phoenix_app` on localhost:5432), `PHOENIX_COMPOSE_PROJECT=integ-phase7-8`, `PHOENIX_EXECUTION_POLICY=guarded|autonomous_lab`, `PYTHONUNBUFFERED=1`.
- Ports: proxy 127.0.0.1:2375, phoenix-api 5000, LLM proxy 4141, Postgres 5432, checkout 8001, payment 8003.
- Docker Desktop has stopped without warning twice; check `docker info` first.
- Git Bash quirk: `docker exec <c> cat /usr/...` is rewritten to a Windows path; use `docker exec <c> sh -c 'cat /usr/...'` or `MSYS_NO_PATHCONV=1`.
- A GateGuard hook demands stated facts before the first Bash/PowerShell command and before first edit or write of each file.
- Current runtime state: all 15 services running, proxy on the pinned image with the custom template, worker-service running, no watcher/driver/traffic processes left, no pending approvals. The user's lab state is reset after each scenario (checkout v17, auth pool 10).
- Scratch outputs: `C:\Users\yashd\AppData\Local\Temp\claude\d--Agentic\922bbc4e-431d-4721-a37a-dba191044ccb\scratchpad\` — `run_scenario.sh`, `env.sh`, `traffic.py`, `runs\<label>_<mode>\` (watcher log, state dump), `runs\s3_autonomous_lab_run1_403\` (first scenario 3 run), `full2.txt` (full suite), `pr37.diff` (Tier 3 patch), `branches_before.txt` (29-branch baseline).

## 13. Git / Repository State

- Worktree `D:\Agentic\.claude\worktrees\integ-phase7-8` on `integration/phase7-8`, HEAD `6a3e741`, pushed. Remote `integration/phase7-8` = `6a3e741`.
- `origin/main` = `e352864` ("report failed reads from a source that also answered in the insufficient-evidence report"), one commit past `84aaa40`. It has diverged from `integration/phase7-8` (1 commit each side). I did not look at what `e352864` changes, and the proxy fix is not in `main`. No merge, rebase or PR was done.
- Uncommitted: `phoenix/tools/docker_tool.py`, `chaos/lib/deployer.py`. Untracked: the four `docs/` reports and this file.
- The main checkout `D:\Agentic` was left alone; the first handoff said it is behind with uncommitted user files.
- Remote leftovers (see section 11). Open PRs on the repo: #25 and #35 (both worker-service, preexisting). Highest PR number is 37 (closed).

## 14. Open Questions

- Commit and push the two docstring edits? (user decides; they authorized only the proxy commit/push)
- Delete the leftover branch and PR #25 comment? Branch only, or comment too? (user decides)
- How to reconcile `integration/phase7-8` with `main` (`e352864`): merge, rebase or PR? Not requested.
- Whether to re-run scenario 2 in guarded mode and Gate 0.
- Whether incident status semantics (alert vs outcome) should change; the list view already shows both.

## 15. Next Steps

1. Run `git diff` on `phoenix/tools/docker_tool.py` and `chaos/lib/deployer.py`, show it to the user, and ask whether to commit them. If yes, one commit, human-style message in the repo's lowercase style, no attribution lines, then ask before pushing.
2. Ask about deleting the leftover branch `phoenix/tier3-incident-19-worker-service-1792482512` and the PR #25 comment above; do it only on an explicit yes.
3. Inspect `e352864` (`git show origin/main`) and tell the user how it relates to `6a3e741`; do not merge without being asked.
4. Fix the possibly stale text listed in section 11 and update the `docs/` reports, only if the user wants them updated.
5. Optional, user's call: scenario 2 guarded, `PHOENIX_RUN_TIER3_E2E=1` tests, Gate 0, and the F4–F15 findings.

## 16. Recommended First Action for the Next Agent

Run `git status --short --branch` and `git diff` in `D:\Agentic\.claude\worktrees\integ-phase7-8`, then ask the user whether to commit the two docstring-only edits (next step 1). Respect these: do not push, merge or delete anything on GitHub without an explicit instruction; do not remove containers, volumes or images; never print `.env` values; the proxy allowlist lives in `infra/docker-socket-proxy/haproxy.cfg.template`, not in env flags, so do not "simplify" it back to `ALLOW_*` or `POST=1`.
