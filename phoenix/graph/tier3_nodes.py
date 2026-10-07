"""Tier 3: code investigation -> patch -> validate -> PR, as four separate
graph nodes, never folded into one.

Each node keeps the same LLM-proposes / code-decides split the rest of the
graph uses: code_investigator and patch_generator are the only two nodes here
that call the LLM, and only to *describe* a defect or *propose* a file body --
never to execute anything. patch_validator and pr_opener make no LLM call at
all; they run real subprocesses (pytest, pyflakes, git, gh) and report exactly
what those subprocesses said. The chain either reaches "PR opened" or stops
with a named reason; there is no path from here to a merge or a push to main.
"""

from __future__ import annotations

import sys

from langgraph.graph import END
from langgraph.types import Command

from phoenix.graph.llm_client import decide_code_investigation_calls, decide_defect, decide_patch
from phoenix.graph.persist import record_audit
from phoenix.graph.schemas import PatchTarget
from phoenix.graph.state import AgentState
from phoenix.tools import git_tool, patch_tool, repo_tool, test_runner_tool, worktree_tool
from phoenix.tools.github_tool import comment_on_pull_request, find_open_duplicate, open_pull_request

TIER3_DESTINATIONS: tuple[str, ...] = ("patch_generator", "patch_validator", "pr_opener", END)

TOOL_DISPATCH_TIER3 = {
    "search_repository": lambda args: repo_tool.search_repository(args["query"], args.get("path")),
    "read_file": lambda args: repo_tool.read_file(args["path"], args.get("start_line"), args.get("end_line")),
    "get_git_commits": lambda args: git_tool.get_git_commits(args.get("path"), args.get("limit", 10)),
    "get_git_diff": lambda args: git_tool.get_git_diff(
        args.get("base", "HEAD~1"), args.get("head", "HEAD"), args.get("path")
    ),
}

PR_BASE_BRANCH = "main"
MAX_PATCH_ATTEMPTS = 3


def _say(message: str = "") -> None:
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        rendered = message.encode(encoding, "backslashreplace").decode(encoding, "replace")
    except (LookupError, UnicodeError):
        rendered = message.encode("ascii", "backslashreplace").decode("ascii")
    print(rendered)


def _escalate(state: AgentState, node: str, event_type: str, reason: str, extra: dict | None = None) -> Command:
    """Stop the run with a named Tier 3 failure, recorded before it stops.

    Mirrors nodes._escalate's contract exactly: the reason lands both in the
    audit row and in the final state, and a run that fails here is recorded
    as a failure, never silently dropped. extra lets each caller attach its
    own tier3_status and whatever partial result (patch_candidate,
    patch_validation, pr_result) it has, so a rejection carries the evidence
    of what was attempted and why it did not continue.
    """
    _say(f"[{node}] {reason} -> end (escalate)")
    record_audit(state.incident_id, node, event_type, {"iteration": state.tier3_iteration, **(extra or {})}, reason)
    return Command(goto=END, update={"status": "escalated", "escalation_reason": reason, **(extra or {})})


def code_investigator_node(state: AgentState) -> Command:
    """LLM proposes which repo/git tool to call next; this code executes
    exactly that and nothing else -- the same discipline observer_node uses
    for its five runtime tools, applied to the four Tier 3 tools.

    Runs its own bounded loop (max_tier3_iterations) rather than looping back
    through the graph, because this sub-investigation is one phase of Tier 3,
    not a state the rest of the graph needs to route around on each
    iteration. It stops either when the LLM asks for no more tools or when
    the cap is hit, then asks once, separately, for a defect conclusion --
    mirroring the observer/diagnoser split at the top level.
    """
    hypothesis = state.hypotheses[0].hypothesis if state.hypotheses else None
    hypothesis_description = hypothesis.description if hypothesis else "no runtime hypothesis was recorded"

    evidence = list(state.tier3_evidence)
    tokens_spent = state.tokens_spent
    iteration = state.tier3_iteration

    # Where the service's code lives, so the model is not left to guess it from a deployment
    # marker. A layout that cannot be read is an emptier prompt, never a stopped investigation.
    try:
        repo_context = repo_tool.repo_context(state.service_name)
    except Exception:  # noqa: BLE001
        repo_context = None
    if repo_context:
        record_audit(
            state.incident_id, "code_investigator", "investigation_context",
            {"service_dir": repo_context.get("service_dir"), "service_file_count": len(repo_context.get("service_files") or [])},
            None,
        )

    while iteration < state.max_tier3_iterations:
        iteration += 1
        decision = decide_code_investigation_calls(state.service_name, hypothesis_description, evidence, repo_context)
        tokens_spent += decision.tokens

        if not decision.calls:
            _say(f"[code_investigator] iteration {iteration}: LLM requested no further tool calls")
            break

        for call in decision.calls:
            tool_fn = TOOL_DISPATCH_TIER3.get(call["name"])
            if tool_fn is None:
                _say(f"[code_investigator] iteration {iteration}: ignoring unrecognized tool '{call['name']}'")
                continue
            try:
                result = tool_fn(call["arguments"])
            except Exception as exc:
                result = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
            item = {"iteration": iteration, "source": call["name"], "summary": f"{call['name']}({call['arguments']})", "raw_data": result}
            evidence.append(item)
            _say(f"[code_investigator] iteration {iteration}: called {call['name']}({call['arguments']})")

        record_audit(
            state.incident_id, "code_investigator", "investigation_pass",
            {"iteration": iteration, "tools_called": [c["name"] for c in decision.calls], "evidence_collected": len(evidence)},
            None,
        )

    defect_decision = decide_defect(state.service_name, hypothesis_description, evidence)
    tokens_spent += defect_decision.tokens
    defect = defect_decision.output
    defect_dict = defect.model_dump()

    record_audit(
        state.incident_id, "code_investigator", "investigation_concluded",
        {
            "iteration": iteration, "defect_found": defect.defect_found, "file_path": defect.file_path,
            "function_name": defect.function_name, "fix_approach": defect.fix_approach,
            "evidence_collected": len(evidence),
        },
        defect.description,
    )

    if not defect.defect_found or not defect.file_path:
        return _escalate(
            state, "code_investigator", "no_defect_found",
            f"Tier 3 investigation could not pin the {state.service_name} defect to one file: {defect.description}",
            {"tier3_status": "no_defect_found", "tier3_evidence": evidence, "tokens_spent": tokens_spent, "tier3_iteration": iteration},
        )

    _say(f"[code_investigator] concluded: {defect.file_path} -- {defect.description}")
    return Command(
        goto="patch_generator",
        update={
            "status": "tier3_investigating",
            "tier3_status": "investigating",
            "tier3_evidence": evidence,
            "tier3_defect": defect_dict,
            "tier3_iteration": iteration,
            "tokens_spent": tokens_spent,
        },
    )


def _patch_target(defect: dict, file_path: str, committed_content: str) -> tuple[PatchTarget | None, str | None]:
    """Build the patch target from the investigator's conclusion, or say why
    there is none. The target function has to be one the investigation named
    and that really exists in the file: with nothing to tie the patch to, the
    gate has nothing to check it against, so the run stops here instead.
    """
    function_name = (defect.get("function_name") or "").strip()
    if not function_name:
        return None, "the investigation did not name a target function, so a patch cannot be tied to the defect"
    if not patch_tool.function_exists(committed_content, function_name):
        return None, f"the investigation's target function '{function_name}' is not defined in '{file_path}'"
    return PatchTarget(
        target_file=file_path,
        target_function=function_name,
        defect_summary=defect.get("description", ""),
        required_change=defect.get("fix_approach", ""),
        forbidden_areas=list(patch_tool.FORBIDDEN_AREAS),
    ), None


def patch_generator_node(state: AgentState) -> Command:
    """LLM proposes the whole new content of one file; this code turns that
    into a diff and runs the deterministic scope gate -- the LLM never
    decides whether its own patch is acceptable.

    The proposal is tied to the function the investigation named: the model
    is given that function as its target and must declare it before patching,
    and the gate rejects a patch that does not change it or that changes any
    other function.
    """
    defect = state.tier3_defect or {}
    file_path = defect.get("file_path")
    if not file_path:
        return _escalate(
            state, "patch_generator", "patch_generation_failed",
            "no file_path was recorded on the defect; there is nothing to patch",
            {"tier3_status": "patch_rejected"},
        )

    current = patch_tool.read_committed(file_path)
    if current.get("status") != "ok":
        return _escalate(
            state, "patch_generator", "patch_generation_failed",
            f"could not read '{file_path}' to propose a patch: {current.get('error')}",
            {"tier3_status": "patch_rejected"},
        )

    target, target_error = _patch_target(defect, file_path, current["content"])
    if target is None:
        return _escalate(
            state, "patch_generator", "patch_generation_failed", target_error,
            {"tier3_status": "patch_rejected"},
        )
    record_audit(state.incident_id, "patch_generator", "patch_target", target.model_dump(), None)

    # The gate's verdict is fed back to the model so a rejected patch can be
    # corrected rather than abandoned, but only a bounded number of times, and
    # every attempt (not just the last) is written to the audit trail: a
    # rejection that was retried and then accepted is still a rejection an
    # operator should be able to see.
    feedback = None
    tokens_spent = state.tokens_spent
    for attempt in range(1, MAX_PATCH_ATTEMPTS + 1):
        patch_decision = decide_patch(current["content"], target, feedback)
        tokens_spent += patch_decision.tokens
        if patch_decision.output is None:
            return _escalate(
                state, "patch_generator", "patch_generation_failed",
                "the LLM produced no usable patch proposal",
                {"tier3_status": "patch_rejected", "tokens_spent": tokens_spent},
            )

        candidate = patch_tool.generate_patch(file_path, patch_decision.output.new_content)
        if candidate.get("status") != "ok":
            return _escalate(
                state, "patch_generator", "patch_generation_failed",
                f"generate_patch refused: {candidate.get('error')}",
                {"tier3_status": "patch_rejected", "tokens_spent": tokens_spent},
            )

        scope_ok, scope_reason = patch_tool.validate_patch_scope(
            candidate["file_path"], candidate["diff"], candidate["changed_lines"], candidate["hunks"],
            target_function=target.target_function,
            old_content=candidate["old_content"],
            new_content=candidate["new_content"],
        )
        declared = patch_decision.output.target_function
        if scope_ok and not patch_tool.same_target(declared, target.target_function):
            scope_ok = False
            scope_reason = (
                f"the patch declares that it changes '{declared or 'no function'}', but the "
                f"investigation's target function is '{target.target_function}'"
            )
        record_audit(
            state.incident_id, "patch_generator", "patch_proposed",
            {
                "attempt": attempt, "file_path": candidate["file_path"], "changed_lines": candidate["changed_lines"],
                "hunks": candidate["hunks"], "scope_ok": scope_ok, "diff": candidate["diff"],
                "target_function": target.target_function, "declared_target_function": declared,
            },
            scope_reason,
        )
        if scope_ok:
            break

        if attempt == MAX_PATCH_ATTEMPTS:
            return _escalate(
                state, "patch_generator", "patch_rejected", scope_reason,
                {
                    "tier3_status": "patch_rejected",
                    "tokens_spent": tokens_spent,
                    "patch_candidate": {**candidate, "rationale": patch_decision.output.rationale, "scope_ok": False, "scope_reason": scope_reason},
                },
            )
        _say(f"[patch_generator] attempt {attempt} rejected by the scope gate ({scope_reason}); asking for a corrected patch")
        feedback = scope_reason

    _say(f"[patch_generator] proposed patch to {candidate['file_path']}: {scope_reason}")
    return Command(
        goto="patch_validator",
        update={
            "tier3_status": "patch_generated",
            "tokens_spent": tokens_spent,
            "patch_candidate": {
                "file_path": candidate["file_path"],
                "diff": candidate["diff"],
                "new_content": candidate["new_content"],
                "changed_lines": candidate["changed_lines"],
                "hunks": candidate["hunks"],
                "rationale": patch_decision.output.rationale,
                "target_function": target.target_function,
                "scope_ok": True,
                "scope_reason": scope_reason,
            },
        },
    )


def patch_validator_node(state: AgentState) -> Command:
    """No LLM call. Apply the candidate in an isolated worktree, run the
    real tests and the real linter, re-inspect the actual committed diff, and
    only then decide the patch is validated. Any failure discards the
    worktree and escalates with the real result attached -- never a fabricated
    pass.
    """
    candidate = state.patch_candidate or {}
    file_path = candidate.get("file_path")
    if not file_path:
        return _escalate(state, "patch_validator", "validation_failed", "no patch candidate to validate", {"tier3_status": "validation_failed"})

    branch = worktree_tool.new_branch_name(state.incident_id, state.service_name)
    created = worktree_tool.create_worktree(branch)
    if created.get("status") != "ok":
        return _escalate(
            state, "patch_validator", "validation_failed",
            f"could not create an isolated worktree: {created.get('error')}",
            {"tier3_status": "validation_failed"},
        )
    worktree_path = created["path"]

    def _fail(reason: str, partial: dict) -> Command:
        worktree_tool.discard_worktree(worktree_path, branch)
        return _escalate(
            state, "patch_validator", "validation_failed", reason,
            {"tier3_status": "validation_failed", "patch_validation": partial},
        )

    written = worktree_tool.write_file_in_worktree(worktree_path, file_path, candidate["new_content"])
    if written.get("status") != "ok":
        return _fail(f"could not write the patch into the worktree: {written.get('error')}", {"applied": False})

    committed = worktree_tool.commit_patch(worktree_path, file_path, f"Tier 3: {candidate.get('rationale', 'patch')[:200]}")
    if committed.get("status") != "ok":
        return _fail(f"could not commit the patch in the worktree: {committed.get('error')}", {"applied": False})
    commit_sha = committed["commit_sha"]

    diff_check = worktree_tool.diff_against_base(worktree_path)
    if diff_check.get("status") != "ok":
        return _fail(f"could not re-inspect the committed diff: {diff_check.get('error')}", {"applied": True, "commit_sha": commit_sha})

    changed_files = diff_check.get("changed_files", [])
    if changed_files != [file_path]:
        return _fail(
            f"the committed diff touches {changed_files}, not exactly ['{file_path}']",
            {"applied": True, "commit_sha": commit_sha, "diff_valid": False},
        )

    base = patch_tool.read_committed(file_path)
    if base.get("status") != "ok":
        return _fail(
            f"could not read the committed base of '{file_path}' to re-check the patch: {base.get('error')}",
            {"applied": True, "commit_sha": commit_sha, "diff_valid": False},
        )
    rescoped_ok, rescoped_reason = patch_tool.validate_patch_scope(
        file_path, diff_check["diff"],
        sum(1 for l in diff_check["diff"].splitlines() if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))),
        sum(1 for l in diff_check["diff"].splitlines() if l.startswith("@@")),
        target_function=candidate.get("target_function"),
        old_content=base["content"],
        new_content=candidate["new_content"],
    )
    if not rescoped_ok:
        return _fail(
            f"the actual committed diff fails scope validation: {rescoped_reason}",
            {"applied": True, "commit_sha": commit_sha, "diff_valid": False},
        )

    test_paths = test_runner_tool.discover_tests_for(file_path)
    test_result = test_runner_tool.run_tests(worktree_path, test_paths)
    lint_result = test_runner_tool.run_linter(worktree_path, file_path)

    tests_passed = test_result.get("status") == "ok"
    lint_passed = lint_result.get("status") == "ok"

    patch_validation = {
        "applied": True,
        "commit_sha": commit_sha,
        "diff_valid": True,
        "scope_ok": True,
        "tests_discovered": test_paths,
        "tests_passed": tests_passed,
        "test_result": test_result,
        "lint_passed": lint_passed,
        "lint_result": lint_result,
        "final_diff": diff_check["diff"],
    }

    record_audit(
        state.incident_id, "patch_validator", "validation_result",
        {k: v for k, v in patch_validation.items() if k != "final_diff"},
        f"tests_passed={tests_passed} lint_passed={lint_passed}",
    )

    if not (tests_passed and lint_passed):
        reason = (
            f"patch rejected: tests_passed={tests_passed} ({test_result.get('error') or test_result.get('returncode')}), "
            f"lint_passed={lint_passed} ({lint_result.get('error') or lint_result.get('returncode')})"
        )
        worktree_tool.discard_worktree(worktree_path, branch)
        return _escalate(
            state, "patch_validator", "validation_failed", reason,
            {"tier3_status": "validation_failed", "patch_validation": patch_validation},
        )

    pushed = worktree_tool.push_branch(worktree_path, branch)
    if pushed.get("status") != "ok":
        worktree_tool.discard_worktree(worktree_path, branch)
        return _escalate(
            state, "patch_validator", "validation_failed",
            f"validated patch could not be pushed: {pushed.get('error')}",
            {"tier3_status": "validation_failed", "patch_validation": patch_validation},
        )

    _say(f"[patch_validator] patch to {file_path} validated (tests and lint both pass) -> pr_opener")
    return Command(
        goto="pr_opener",
        update={
            "tier3_status": "validated",
            "patch_validation": patch_validation,
            "worktree_path": worktree_path,
            "worktree_branch": branch,
        },
    )


def pr_opener_node(state: AgentState) -> Command:
    """No LLM call. Opens a real PR through the authenticated `gh` CLI only
    when every validation flag the PRD names is true, and the diff attached
    to the PR is the real committed diff -- never the candidate's.
    """
    validation = state.patch_validation or {}
    required = ("scope_ok", "applied", "tests_passed", "lint_passed", "diff_valid")
    if not all(validation.get(flag) for flag in required):
        missing = [flag for flag in required if not validation.get(flag)]
        return _escalate(
            state, "pr_opener", "pr_blocked",
            f"refusing to open a PR: validation flags not all true (missing: {missing})",
            {"tier3_status": "pr_failed"},
        )

    candidate = state.patch_candidate or {}
    defect = state.tier3_defect or {}
    hypothesis = state.hypotheses[0].hypothesis if state.hypotheses else None

    title = f"[Phoenix Tier 3] incident #{state.incident_id}: fix {defect.get('file_path', state.service_name)}"
    body = (
        f"**Incident:** #{state.incident_id}  \n"
        f"**Service:** {state.service_name}  \n"
        f"**Root-cause hypothesis:** {hypothesis.category if hypothesis else 'unknown'} -- "
        f"{hypothesis.description if hypothesis else 'n/a'}\n\n"
        f"**Defect:** {defect.get('description', 'n/a')}\n\n"
        f"**Fix:** {candidate.get('rationale', 'n/a')}\n\n"
        f"**File changed:** `{candidate.get('file_path')}` "
        f"({candidate.get('changed_lines')} lines, {candidate.get('hunks')} hunk(s))\n\n"
        f"**Tests run:** {', '.join(validation.get('tests_discovered') or []) or 'none discovered'} "
        f"-- passed: {validation.get('tests_passed')}\n\n"
        f"**Lint:** pyflakes -- passed: {validation.get('lint_passed')}\n\n"
        f"**Commit:** {validation.get('commit_sha')}\n\n"
        "---\n"
        "Opened autonomously by Phoenix Tier 3. **This PR requires human review and "
        "must not be merged automatically.**\n\n"
        "```diff\n"
        f"{(validation.get('final_diff') or '')[:5000]}\n"
        "```"
    )

    # A recurring incident arrives with the same fix an open PR already proposes.
    # Opening it again only makes the reviewer read it twice; the existing PR is
    # told it happened again instead. A lookup that fails is not an answer of "no
    # duplicate", so it is recorded, and the PR is opened as it always was.
    lookup = find_open_duplicate(validation.get("final_diff") or "")
    existing = lookup.get("duplicate") if lookup.get("status") == "ok" else None
    if lookup.get("status") != "ok":
        record_audit(
            state.incident_id, "pr_opener", "pr_duplicate_check_failed", {"error": lookup.get("error")},
            "could not check for an existing PR with this change; opening one",
        )
    if existing:
        comment = comment_on_pull_request(
            existing["number"],
            f"Phoenix produced this same fix again for incident #{state.incident_id} "
            f"({state.service_name}), so no new PR was opened. The fault recurred; "
            f"this PR is still the open proposal.",
        )
        record_audit(
            state.incident_id, "pr_opener", "pr_duplicate",
            {"existing_pr": existing, "comment": comment, "branch": state.worktree_branch},
            f"an open PR (#{existing['number']}) already carries this change; no new PR opened",
        )
        if state.worktree_path:
            worktree_tool.discard_worktree(state.worktree_path, state.worktree_branch)
        _say(f"[pr_opener] {existing['url']} already carries this change -> no new PR, STOP for human review")
        return Command(
            goto=END,
            update={
                "status": "pr_exists", "tier3_status": "pr_exists",
                "pr_result": {"status": "exists", "url": existing["url"], "duplicate_of": existing["number"],
                              "branch": existing.get("branch")},
            },
        )

    result = open_pull_request(state.worktree_branch, title, body, base=worktree_tool.current_branch() or PR_BASE_BRANCH)
    record_audit(state.incident_id, "pr_opener", "pr_result", {"result": result}, None)

    if state.worktree_path:
        worktree_tool.discard_worktree(state.worktree_path, state.worktree_branch)

    if result.get("status") != "ok":
        return _escalate(
            state, "pr_opener", "pr_failed", f"gh pr create failed: {result.get('error')}",
            {"tier3_status": "pr_failed", "pr_result": result},
        )

    _say(f"[pr_opener] opened {result['url']} -> STOP for human review")
    return Command(goto=END, update={"status": "pr_opened", "tier3_status": "pr_opened", "pr_result": result})
