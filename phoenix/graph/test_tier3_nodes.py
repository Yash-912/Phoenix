"""Safety-boundary unit tests for the Tier 3 subgraph. Every LLM decision and
every real-subprocess tool call is monkeypatched so these run fast, offline,
and without ever creating a real PR or pushing a real branch -- that is the
Tier 3 E2E integration tests' job, not this file's.
"""

import os

os.environ.setdefault("LLM_BASE_URL", "http://localhost:1/v1")
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_MODEL", "test-model")

import pytest
from langgraph.graph import END

import phoenix.graph.tier3_nodes as tier3_nodes
from phoenix.graph.llm_client import DefectDecision, PatchDecision, ToolCallDecision
from phoenix.graph.schemas import CodeDefect, Hypothesis, PatchProposal
from phoenix.graph.state import AgentState

SERVICE = "payment-service"


def _state(**overrides) -> AgentState:
    fields = {
        "incident_id": 1,
        "service_name": SERVICE,
        "hypotheses": [
            {
                "hypothesis": Hypothesis(description="payment /charge is slow", category="slow_query"),
                "score": 0.8,
                "score_breakdown": {},
            }
        ],
    }
    fields.update(overrides)
    return AgentState(**fields)


def _update(command):
    return command.update or {}


# --- code_investigator_node ---------------------------------------------


def test_insufficient_evidence_escalates_without_a_defect(monkeypatch):
    """Ambiguous diagnosis: the investigator exhausts its tool-call budget
    and the LLM honestly reports it could not pin the defect to one file."""
    monkeypatch.setattr(tier3_nodes, "decide_code_investigation_calls", lambda *a: ToolCallDecision([], 10))
    monkeypatch.setattr(
        tier3_nodes, "decide_defect",
        lambda *a: DefectDecision(
            CodeDefect(defect_found=False, description="evidence was ambiguous", fix_approach="n/a", confidence_rationale="n/a"),
            5,
        ),
    )
    monkeypatch.setattr(tier3_nodes, "record_audit", lambda *a, **k: None)

    command = tier3_nodes.code_investigator_node(_state())
    update = _update(command)

    assert command.goto == END
    assert update["status"] == "escalated"
    assert update["tier3_status"] == "no_defect_found"


def test_investigation_loop_stops_at_the_iteration_cap(monkeypatch):
    calls_made = []
    monkeypatch.setattr(
        tier3_nodes, "decide_code_investigation_calls",
        lambda *a: (calls_made.append(1), ToolCallDecision([{"name": "search_repository", "arguments": {"query": "x"}}], 1))[1],
    )
    monkeypatch.setattr(tier3_nodes, "decide_defect", lambda *a: DefectDecision(
        CodeDefect(defect_found=False, description="n/a", fix_approach="n/a", confidence_rationale="n/a"), 0
    ))
    monkeypatch.setattr(tier3_nodes, "record_audit", lambda *a, **k: None)
    monkeypatch.setitem(tier3_nodes.TOOL_DISPATCH_TIER3, "search_repository", lambda args: {"status": "ok", "matches": []})

    state = _state(max_tier3_iterations=3)
    tier3_nodes.code_investigator_node(state)

    assert len(calls_made) == 3


def test_a_found_defect_hands_off_to_patch_generator(monkeypatch):
    monkeypatch.setattr(tier3_nodes, "decide_code_investigation_calls", lambda *a: ToolCallDecision([], 0))
    monkeypatch.setattr(
        tier3_nodes, "decide_defect",
        lambda *a: DefectDecision(
            CodeDefect(
                defect_found=True, file_path="services/payment-service/app.py", function_name="_find_charge_slow",
                description="full table scan", fix_approach="use an indexed lookup", confidence_rationale="evidence supports it",
            ),
            0,
        ),
    )
    monkeypatch.setattr(tier3_nodes, "record_audit", lambda *a, **k: None)

    command = tier3_nodes.code_investigator_node(_state())
    update = _update(command)

    assert command.goto == "patch_generator"
    assert update["tier3_defect"]["file_path"] == "services/payment-service/app.py"


# --- patch_generator_node ------------------------------------------------


def test_no_file_path_on_the_defect_escalates(monkeypatch):
    monkeypatch.setattr(tier3_nodes, "record_audit", lambda *a, **k: None)
    state = _state(tier3_defect={"defect_found": True, "file_path": None, "description": "x", "fix_approach": "y"})

    command = tier3_nodes.patch_generator_node(state)

    assert command.goto == END
    assert _update(command)["tier3_status"] == "patch_rejected"


def test_model_proposes_a_nonexistent_file(monkeypatch):
    monkeypatch.setattr(tier3_nodes, "record_audit", lambda *a, **k: None)
    state = _state(tier3_defect={
        "defect_found": True, "file_path": "services/does-not-exist/app.py",
        "description": "x", "fix_approach": "y",
    })

    command = tier3_nodes.patch_generator_node(state)

    assert command.goto == END
    assert _update(command)["tier3_status"] == "patch_rejected"


def test_llm_produces_no_usable_patch_proposal(monkeypatch):
    monkeypatch.setattr(tier3_nodes, "decide_patch", lambda *a: PatchDecision(None, 3))
    monkeypatch.setattr(tier3_nodes, "record_audit", lambda *a, **k: None)
    state = _state(tier3_defect={
        "defect_found": True, "file_path": "services/payment-service/app.py", "function_name": "_find_charge_slow",
        "description": "x", "fix_approach": "y",
    })

    command = tier3_nodes.patch_generator_node(state)

    assert command.goto == END
    assert _update(command)["tier3_status"] == "patch_rejected"


def test_model_proposes_a_chaos_toggle_change_instead_of_the_defect(monkeypatch):
    """The exact failure mode the PRD calls out: a 'fix' that flips
    SLOW_QUERY rather than touching the query implementation."""
    from phoenix.tools import patch_tool

    current = patch_tool.read_committed("services/payment-service/app.py")["content"]
    sabotage = current.replace("SLOW_QUERY = False", "SLOW_QUERY = True")

    monkeypatch.setattr(tier3_nodes, "decide_patch", lambda *a: PatchDecision(PatchProposal(new_content=sabotage, rationale="flip the flag", target_function="_find_charge_slow"), 3))
    monkeypatch.setattr(tier3_nodes, "record_audit", lambda *a, **k: None)
    state = _state(tier3_defect={
        "defect_found": True, "file_path": "services/payment-service/app.py", "function_name": "_find_charge_slow",
        "description": "slow query", "fix_approach": "fix it",
    })

    command = tier3_nodes.patch_generator_node(state)
    update = _update(command)

    assert command.goto == END
    assert update["tier3_status"] == "patch_rejected"
    assert "chaos toggle" in update["escalation_reason"]


def test_a_minimal_in_scope_patch_proceeds_to_the_validator(monkeypatch):
    from phoenix.tools import patch_tool

    current = patch_tool.read_committed("services/payment-service/app.py")["content"]
    fixed = current.replace(
        'cur.execute("SELECT order_id, amount, status FROM charges")',
        'cur.execute("SELECT amount, status FROM charges WHERE order_id = %s", (order_id,))',
    )
    assert fixed != current

    monkeypatch.setattr(tier3_nodes, "decide_patch", lambda *a: PatchDecision(PatchProposal(new_content=fixed, rationale="use the indexed lookup", target_function="_find_charge_slow"), 3))
    monkeypatch.setattr(tier3_nodes, "record_audit", lambda *a, **k: None)
    state = _state(tier3_defect={
        "defect_found": True, "file_path": "services/payment-service/app.py", "function_name": "_find_charge_slow",
        "description": "slow query", "fix_approach": "fix it",
    })

    command = tier3_nodes.patch_generator_node(state)
    update = _update(command)

    assert command.goto == "patch_validator"
    assert update["patch_candidate"]["scope_ok"] is True
    assert update["patch_candidate"]["target_function"] == "_find_charge_slow"


# --- patch_validator_node ------------------------------------------------


def _candidate_state(**overrides):
    fields = {"patch_candidate": {
        "file_path": "services/payment-service/app.py",
        "new_content": "x = 1\n",
        "diff": "@@ -1 +1 @@\n-old\n+new\n",
        "changed_lines": 2,
        "hunks": 1,
        "rationale": "test",
        "target_function": "_find_charge_slow",
    }}
    fields.update(overrides)
    return _state(**fields)


def test_worktree_creation_failure_escalates(monkeypatch):
    monkeypatch.setattr(tier3_nodes.worktree_tool, "new_branch_name", lambda *a: "phoenix/test-x")
    monkeypatch.setattr(tier3_nodes.worktree_tool, "create_worktree", lambda b: {"status": "error", "error": "disk full"})
    monkeypatch.setattr(tier3_nodes, "record_audit", lambda *a, **k: None)

    command = tier3_nodes.patch_validator_node(_candidate_state())
    update = _update(command)

    assert command.goto == END
    assert update["tier3_status"] == "validation_failed"
    assert "disk full" in update["escalation_reason"]


def test_patch_that_fails_to_apply_is_rejected_and_worktree_discarded(monkeypatch):
    discarded = []
    monkeypatch.setattr(tier3_nodes.worktree_tool, "new_branch_name", lambda *a: "phoenix/test-x")
    monkeypatch.setattr(tier3_nodes.worktree_tool, "create_worktree", lambda b: {"status": "ok", "path": "/fake/path", "branch": b})
    monkeypatch.setattr(tier3_nodes.worktree_tool, "write_file_in_worktree", lambda *a: {"status": "error", "error": "permission denied"})
    monkeypatch.setattr(tier3_nodes.worktree_tool, "discard_worktree", lambda p, b: discarded.append((p, b)))
    monkeypatch.setattr(tier3_nodes, "record_audit", lambda *a, **k: None)

    command = tier3_nodes.patch_validator_node(_candidate_state())
    update = _update(command)

    assert command.goto == END
    assert update["tier3_status"] == "validation_failed"
    assert discarded == [("/fake/path", "phoenix/test-x")]


def test_a_diff_touching_more_than_one_file_is_rejected(monkeypatch):
    monkeypatch.setattr(tier3_nodes.worktree_tool, "new_branch_name", lambda *a: "phoenix/test-x")
    monkeypatch.setattr(tier3_nodes.worktree_tool, "create_worktree", lambda b: {"status": "ok", "path": "/fake/path", "branch": b})
    monkeypatch.setattr(tier3_nodes.worktree_tool, "write_file_in_worktree", lambda *a: {"status": "ok"})
    monkeypatch.setattr(tier3_nodes.worktree_tool, "commit_patch", lambda *a: {"status": "ok", "commit_sha": "a" * 40})
    monkeypatch.setattr(
        tier3_nodes.worktree_tool, "diff_against_base",
        lambda *a, **k: {"status": "ok", "diff": "...", "changed_files": ["services/payment-service/app.py", "services/worker-service/app.py"]},
    )
    discarded = []
    monkeypatch.setattr(tier3_nodes.worktree_tool, "discard_worktree", lambda p, b: discarded.append((p, b)))
    monkeypatch.setattr(tier3_nodes, "record_audit", lambda *a, **k: None)

    command = tier3_nodes.patch_validator_node(_candidate_state())
    update = _update(command)

    assert command.goto == END
    assert update["tier3_status"] == "validation_failed"
    assert "not exactly" in update["escalation_reason"]
    assert discarded


def test_failing_tests_reject_the_patch(monkeypatch):
    monkeypatch.setattr(tier3_nodes.worktree_tool, "new_branch_name", lambda *a: "phoenix/test-x")
    monkeypatch.setattr(tier3_nodes.worktree_tool, "create_worktree", lambda b: {"status": "ok", "path": "/fake/path", "branch": b})
    monkeypatch.setattr(tier3_nodes.worktree_tool, "write_file_in_worktree", lambda *a: {"status": "ok"})
    monkeypatch.setattr(tier3_nodes.worktree_tool, "commit_patch", lambda *a: {"status": "ok", "commit_sha": "a" * 40})
    monkeypatch.setattr(
        tier3_nodes.worktree_tool, "diff_against_base",
        lambda *a, **k: {"status": "ok", "diff": "@@ -1 +1 @@\n-a\n+b\n", "changed_files": ["services/payment-service/app.py"]},
    )
    monkeypatch.setattr(tier3_nodes.patch_tool, "validate_patch_scope", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(tier3_nodes.test_runner_tool, "discover_tests_for", lambda *a: ["phoenix/test_payment_service_charge.py"])
    monkeypatch.setattr(tier3_nodes.test_runner_tool, "run_tests", lambda *a: {"status": "failed", "returncode": 1, "stdout": "2 failed"})
    monkeypatch.setattr(tier3_nodes.test_runner_tool, "run_linter", lambda *a: {"status": "ok", "returncode": 0})
    discarded = []
    monkeypatch.setattr(tier3_nodes.worktree_tool, "discard_worktree", lambda p, b: discarded.append((p, b)))
    monkeypatch.setattr(tier3_nodes, "record_audit", lambda *a, **k: None)

    command = tier3_nodes.patch_validator_node(_candidate_state())
    update = _update(command)

    assert command.goto == END
    assert update["tier3_status"] == "validation_failed"
    assert update["patch_validation"]["tests_passed"] is False
    assert discarded


def test_failing_lint_rejects_the_patch(monkeypatch):
    monkeypatch.setattr(tier3_nodes.worktree_tool, "new_branch_name", lambda *a: "phoenix/test-x")
    monkeypatch.setattr(tier3_nodes.worktree_tool, "create_worktree", lambda b: {"status": "ok", "path": "/fake/path", "branch": b})
    monkeypatch.setattr(tier3_nodes.worktree_tool, "write_file_in_worktree", lambda *a: {"status": "ok"})
    monkeypatch.setattr(tier3_nodes.worktree_tool, "commit_patch", lambda *a: {"status": "ok", "commit_sha": "a" * 40})
    monkeypatch.setattr(
        tier3_nodes.worktree_tool, "diff_against_base",
        lambda *a, **k: {"status": "ok", "diff": "@@ -1 +1 @@\n-a\n+b\n", "changed_files": ["services/payment-service/app.py"]},
    )
    monkeypatch.setattr(tier3_nodes.patch_tool, "validate_patch_scope", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(tier3_nodes.test_runner_tool, "discover_tests_for", lambda *a: ["phoenix/test_payment_service_charge.py"])
    monkeypatch.setattr(tier3_nodes.test_runner_tool, "run_tests", lambda *a: {"status": "ok", "returncode": 0})
    monkeypatch.setattr(tier3_nodes.test_runner_tool, "run_linter", lambda *a: {"status": "failed", "returncode": 1, "stdout": "undefined name 'x'"})
    discarded = []
    monkeypatch.setattr(tier3_nodes.worktree_tool, "discard_worktree", lambda p, b: discarded.append((p, b)))
    monkeypatch.setattr(tier3_nodes, "record_audit", lambda *a, **k: None)

    command = tier3_nodes.patch_validator_node(_candidate_state())
    update = _update(command)

    assert command.goto == END
    assert update["tier3_status"] == "validation_failed"
    assert update["patch_validation"]["lint_passed"] is False
    assert discarded


def test_a_fully_passing_patch_pushes_and_hands_off_to_pr_opener(monkeypatch):
    monkeypatch.setattr(tier3_nodes.worktree_tool, "new_branch_name", lambda *a: "phoenix/test-x")
    monkeypatch.setattr(tier3_nodes.worktree_tool, "create_worktree", lambda b: {"status": "ok", "path": "/fake/path", "branch": b})
    monkeypatch.setattr(tier3_nodes.worktree_tool, "write_file_in_worktree", lambda *a: {"status": "ok"})
    monkeypatch.setattr(tier3_nodes.worktree_tool, "commit_patch", lambda *a: {"status": "ok", "commit_sha": "a" * 40})
    monkeypatch.setattr(
        tier3_nodes.worktree_tool, "diff_against_base",
        lambda *a, **k: {"status": "ok", "diff": "@@ -1 +1 @@\n-a\n+b\n", "changed_files": ["services/payment-service/app.py"]},
    )
    monkeypatch.setattr(tier3_nodes.patch_tool, "validate_patch_scope", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(tier3_nodes.test_runner_tool, "discover_tests_for", lambda *a: ["phoenix/test_payment_service_charge.py"])
    monkeypatch.setattr(tier3_nodes.test_runner_tool, "run_tests", lambda *a: {"status": "ok", "returncode": 0})
    monkeypatch.setattr(tier3_nodes.test_runner_tool, "run_linter", lambda *a: {"status": "ok", "returncode": 0})
    pushed = []
    monkeypatch.setattr(tier3_nodes.worktree_tool, "push_branch", lambda p, b: (pushed.append((p, b)), {"status": "ok", "branch": b})[1])
    monkeypatch.setattr(tier3_nodes, "record_audit", lambda *a, **k: None)

    command = tier3_nodes.patch_validator_node(_candidate_state())
    update = _update(command)

    assert command.goto == "pr_opener"
    assert update["tier3_status"] == "validated"
    assert pushed == [("/fake/path", "phoenix/test-x")]
    assert update["patch_validation"]["tests_passed"] is True
    assert update["patch_validation"]["lint_passed"] is True


# --- pr_opener_node -------------------------------------------------------


def test_pr_opener_refuses_when_validation_flags_are_not_all_true(monkeypatch):
    monkeypatch.setattr(tier3_nodes, "record_audit", lambda *a, **k: None)
    state = _state(patch_validation={"scope_ok": True, "applied": True, "tests_passed": False, "lint_passed": True, "diff_valid": True})

    command = tier3_nodes.pr_opener_node(state)
    update = _update(command)

    assert command.goto == END
    assert update["tier3_status"] == "pr_failed"
    assert "tests_passed" in update["escalation_reason"]


def test_pr_creation_failure_is_recorded_not_hidden(monkeypatch):
    monkeypatch.setattr(tier3_nodes, "open_pull_request", lambda *a, **k: {"status": "error", "error": "API rate limited"})
    monkeypatch.setattr(tier3_nodes.worktree_tool, "discard_worktree", lambda *a: None)
    monkeypatch.setattr(tier3_nodes, "record_audit", lambda *a, **k: None)
    state = _state(
        patch_validation={"scope_ok": True, "applied": True, "tests_passed": True, "lint_passed": True, "diff_valid": True, "commit_sha": "a" * 40},
        patch_candidate={"file_path": "services/payment-service/app.py", "rationale": "fix", "changed_lines": 2, "hunks": 1},
        worktree_branch="phoenix/test-x", worktree_path="/fake/path",
    )

    command = tier3_nodes.pr_opener_node(state)
    update = _update(command)

    assert command.goto == END
    assert update["status"] == "escalated"
    assert update["tier3_status"] == "pr_failed"
    assert update["pr_result"]["error"] == "API rate limited"


def test_pr_opener_never_fabricates_success_and_never_merges(monkeypatch):
    real_url = "https://github.com/Yash-912/Phoenix/pull/999"
    monkeypatch.setattr(tier3_nodes, "open_pull_request", lambda *a, **k: {"status": "ok", "url": real_url, "branch": a[0], "base": "main"})
    discarded = []
    monkeypatch.setattr(tier3_nodes.worktree_tool, "discard_worktree", lambda p, b: discarded.append((p, b)))
    monkeypatch.setattr(tier3_nodes, "record_audit", lambda *a, **k: None)
    state = _state(
        patch_validation={"scope_ok": True, "applied": True, "tests_passed": True, "lint_passed": True, "diff_valid": True, "commit_sha": "a" * 40},
        patch_candidate={"file_path": "services/payment-service/app.py", "rationale": "fix", "changed_lines": 2, "hunks": 1},
        worktree_branch="phoenix/test-x", worktree_path="/fake/path",
    )

    command = tier3_nodes.pr_opener_node(state)
    update = _update(command)

    assert command.goto == END
    assert update["status"] == "pr_opened"
    assert update["tier3_status"] == "pr_opened"
    assert update["pr_result"]["url"] == real_url
    assert discarded == [("/fake/path", "phoenix/test-x")]


# --- pr_opener_node idempotency -------------------------------------------


@pytest.fixture(autouse=True)
def _no_open_duplicate_by_default(monkeypatch):
    """The lookup shells out to gh; no test here may reach the real repo."""
    monkeypatch.setattr(tier3_nodes, "find_open_duplicate", lambda *a, **k: {"status": "ok", "duplicate": None})
    monkeypatch.setattr(tier3_nodes, "comment_on_pull_request", lambda *a, **k: {"status": "ok"})


EXISTING = {"number": 25, "url": "https://github.com/Yash-912/Phoenix/pull/25", "branch": "phoenix/older"}
FINAL_DIFF = "diff --git a/services/payment-service/app.py b/services/payment-service/app.py\n@@ -1 +1 @@\n-a\n+b\n"


def _opener_state():
    return _state(
        incident_id=9,
        patch_validation={"scope_ok": True, "applied": True, "tests_passed": True, "lint_passed": True,
                          "diff_valid": True, "commit_sha": "a" * 40, "final_diff": FINAL_DIFF},
        patch_candidate={"file_path": "services/payment-service/app.py", "rationale": "fix", "changed_lines": 2, "hunks": 1},
        worktree_branch="phoenix/test-x", worktree_path="/fake/path",
    )


def _opener_wiring(monkeypatch):
    rows, created, discarded, comments = [], [], [], []
    monkeypatch.setattr(tier3_nodes, "record_audit", lambda incident, node, event, detail, text: rows.append((event, detail)))
    monkeypatch.setattr(tier3_nodes, "open_pull_request", lambda *a, **k: (created.append(a), {"status": "ok", "url": "https://github.com/x/y/pull/99", "branch": a[0], "base": "main"})[1])
    monkeypatch.setattr(tier3_nodes.worktree_tool, "discard_worktree", lambda p, b: discarded.append((p, b)))
    monkeypatch.setattr(tier3_nodes, "comment_on_pull_request", lambda number, body: (comments.append((number, body)), {"status": "ok"})[1])
    return rows, created, discarded, comments


def test_a_change_an_open_pr_already_carries_opens_no_second_pr(monkeypatch):
    rows, created, discarded, comments = _opener_wiring(monkeypatch)
    monkeypatch.setattr(tier3_nodes, "find_open_duplicate", lambda *a, **k: {"status": "ok", "duplicate": EXISTING})

    command = tier3_nodes.pr_opener_node(_opener_state())
    update = _update(command)

    assert created == []
    assert command.goto == END
    assert update["status"] == "pr_exists"
    assert update["tier3_status"] == "pr_exists"
    assert update["pr_result"]["url"] == EXISTING["url"]
    assert update["pr_result"]["duplicate_of"] == 25
    assert discarded == [("/fake/path", "phoenix/test-x")]


def test_the_existing_pr_is_commented_on_and_the_audit_trail_records_it(monkeypatch):
    rows, created, discarded, comments = _opener_wiring(monkeypatch)
    monkeypatch.setattr(tier3_nodes, "find_open_duplicate", lambda *a, **k: {"status": "ok", "duplicate": EXISTING})

    tier3_nodes.pr_opener_node(_opener_state())

    assert [number for number, _ in comments] == [25]
    assert "incident #9" in comments[0][1]
    events = [event for event, _ in rows]
    assert "pr_duplicate" in events
    assert "pr_result" not in events
    detail = dict(rows)["pr_duplicate"]
    assert detail["existing_pr"] == EXISTING and detail["comment"] == {"status": "ok"}


def test_a_failed_comment_does_not_undo_the_decision_not_to_duplicate(monkeypatch):
    rows, created, discarded, comments = _opener_wiring(monkeypatch)
    monkeypatch.setattr(tier3_nodes, "find_open_duplicate", lambda *a, **k: {"status": "ok", "duplicate": EXISTING})
    monkeypatch.setattr(tier3_nodes, "comment_on_pull_request", lambda *a: {"status": "error", "error": "no permission"})

    command = tier3_nodes.pr_opener_node(_opener_state())

    assert created == []
    assert _update(command)["status"] == "pr_exists"
    assert dict(rows)["pr_duplicate"]["comment"]["error"] == "no permission"


def test_the_duplicate_check_is_made_on_the_real_committed_diff(monkeypatch):
    _opener_wiring(monkeypatch)
    seen = []
    monkeypatch.setattr(tier3_nodes, "find_open_duplicate", lambda diff: (seen.append(diff), {"status": "ok", "duplicate": None})[1])

    tier3_nodes.pr_opener_node(_opener_state())

    assert seen == [FINAL_DIFF]


def test_with_no_duplicate_the_pr_is_opened_as_before(monkeypatch):
    rows, created, discarded, comments = _opener_wiring(monkeypatch)

    update = _update(tier3_nodes.pr_opener_node(_opener_state()))

    assert len(created) == 1 and comments == []
    assert update["status"] == "pr_opened"


def test_a_failed_lookup_still_opens_the_pr_and_says_the_check_failed(monkeypatch):
    rows, created, discarded, comments = _opener_wiring(monkeypatch)
    monkeypatch.setattr(tier3_nodes, "find_open_duplicate", lambda *a, **k: {"status": "error", "error": "gh down"})

    update = _update(tier3_nodes.pr_opener_node(_opener_state()))

    assert len(created) == 1
    assert update["status"] == "pr_opened"
    assert dict(rows)["pr_duplicate_check_failed"]["error"] == "gh down"


def test_a_pr_that_fails_validation_never_reaches_the_duplicate_check(monkeypatch):
    monkeypatch.setattr(tier3_nodes, "record_audit", lambda *a, **k: None)
    monkeypatch.setattr(tier3_nodes, "find_open_duplicate", lambda *a, **k: (_ for _ in ()).throw(AssertionError("looked up")))
    state = _state(patch_validation={"scope_ok": True, "applied": True, "tests_passed": False, "lint_passed": True, "diff_valid": True})

    assert _update(tier3_nodes.pr_opener_node(state))["tier3_status"] == "pr_failed"


# --- structural hard safety boundary --------------------------------------


def test_no_merge_or_direct_main_push_capability_exists_anywhere_in_tier3():
    """Structural, not behavioral: there must be no function Tier 3 could
    call to merge a PR or push to main, not merely a flag that refuses to."""
    import inspect

    from phoenix.tools import github_tool, worktree_tool

    github_names = {name for name, _ in inspect.getmembers(github_tool, inspect.isfunction)}

    assert not any("merge" in name.lower() for name in github_names)
    assert not any("approve" in name.lower() for name in github_names)
    assert "main" in worktree_tool.PROTECTED_BRANCHES and "master" in worktree_tool.PROTECTED_BRANCHES


# --- patch_generator_node retry loop ---------------------------------------


def _defect_state():
    return _state(tier3_defect={
        "defect_found": True, "file_path": "services/payment-service/app.py", "function_name": "_find_charge_slow",
        "description": "slow query", "fix_approach": "fix it",
    })


def _sabotage_and_fix():
    from phoenix.tools import patch_tool

    current = patch_tool.read_committed("services/payment-service/app.py")["content"]
    sabotage = current.replace("SLOW_QUERY = False", "SLOW_QUERY = True")
    fixed = current.replace(
        'cur.execute("SELECT order_id, amount, status FROM charges")',
        'cur.execute("SELECT amount, status FROM charges WHERE order_id = %s", (order_id,))',
    )
    assert sabotage != current and fixed != current
    return sabotage, fixed


def test_a_rejected_patch_is_retried_with_the_gates_reason_and_every_attempt_is_audited(monkeypatch):
    sabotage, fixed = _sabotage_and_fix()
    proposals = iter([sabotage, fixed])
    feedback_seen = []
    audit_rows = []

    def fake_decide_patch(old_content, target, feedback=None):
        feedback_seen.append(feedback)
        return PatchDecision(PatchProposal(new_content=next(proposals), rationale="r", target_function="_find_charge_slow"), 1)

    monkeypatch.setattr(tier3_nodes, "decide_patch", fake_decide_patch)
    monkeypatch.setattr(tier3_nodes, "record_audit", lambda *a, **k: audit_rows.append(a))

    command = tier3_nodes.patch_generator_node(_defect_state())

    assert command.goto == "patch_validator"
    assert feedback_seen[0] is None
    assert "chaos toggle" in feedback_seen[1]
    proposed = [row for row in audit_rows if row[2] == "patch_proposed"]
    assert [row[3]["attempt"] for row in proposed] == [1, 2]
    assert [row[3]["scope_ok"] for row in proposed] == [False, True]


def test_a_patch_rejected_on_every_attempt_escalates_after_the_attempt_cap(monkeypatch):
    sabotage, _fixed = _sabotage_and_fix()
    calls = []
    audit_rows = []

    def fake_decide_patch(*args):
        calls.append(args)
        return PatchDecision(PatchProposal(new_content=sabotage, rationale="r", target_function="_find_charge_slow"), 1)

    monkeypatch.setattr(tier3_nodes, "decide_patch", fake_decide_patch)
    monkeypatch.setattr(tier3_nodes, "record_audit", lambda *a, **k: audit_rows.append(a))

    command = tier3_nodes.patch_generator_node(_defect_state())
    update = _update(command)

    assert command.goto == END
    assert len(calls) == tier3_nodes.MAX_PATCH_ATTEMPTS
    assert update["tier3_status"] == "patch_rejected"
    assert "chaos toggle" in update["escalation_reason"]
    proposed = [row for row in audit_rows if row[2] == "patch_proposed"]
    assert len(proposed) == tier3_nodes.MAX_PATCH_ATTEMPTS


# --- the patch is tied to the function the investigation named --------------


def _real_app_source():
    from phoenix.tools import patch_tool

    return patch_tool.read_committed("services/payment-service/app.py")["content"]


def test_the_patch_generator_is_handed_the_investigations_target(monkeypatch):
    _sabotage, fixed = _sabotage_and_fix()
    seen = []

    def fake_decide_patch(old_content, target, feedback=None):
        seen.append(target)
        return PatchDecision(PatchProposal(new_content=fixed, rationale="r", target_function="_find_charge_slow"), 1)

    monkeypatch.setattr(tier3_nodes, "decide_patch", fake_decide_patch)
    monkeypatch.setattr(tier3_nodes, "record_audit", lambda *a, **k: None)

    tier3_nodes.patch_generator_node(_defect_state())

    target = seen[0]
    assert target.target_file == "services/payment-service/app.py"
    assert target.target_function == "_find_charge_slow"
    assert target.defect_summary == "slow query"
    assert target.required_change == "fix it"
    assert any("dispatcher" in area for area in target.forbidden_areas)
    assert any("toggle" in area for area in target.forbidden_areas)


def test_a_defect_without_a_named_function_escalates_before_any_patch_is_requested(monkeypatch):
    requested = []
    monkeypatch.setattr(tier3_nodes, "decide_patch", lambda *a: requested.append(a))
    monkeypatch.setattr(tier3_nodes, "record_audit", lambda *a, **k: None)
    state = _state(tier3_defect={
        "defect_found": True, "file_path": "services/payment-service/app.py", "function_name": None,
        "description": "x", "fix_approach": "y",
    })

    command = tier3_nodes.patch_generator_node(state)
    update = _update(command)

    assert command.goto == END
    assert update["tier3_status"] == "patch_rejected"
    assert "did not name a target function" in update["escalation_reason"]
    assert requested == []


def test_a_named_function_that_is_not_in_the_file_escalates_before_any_patch_is_requested(monkeypatch):
    requested = []
    monkeypatch.setattr(tier3_nodes, "decide_patch", lambda *a: requested.append(a))
    monkeypatch.setattr(tier3_nodes, "record_audit", lambda *a, **k: None)
    state = _state(tier3_defect={
        "defect_found": True, "file_path": "services/payment-service/app.py", "function_name": "no_such_function",
        "description": "x", "fix_approach": "y",
    })

    command = tier3_nodes.patch_generator_node(state)
    update = _update(command)

    assert command.goto == END
    assert update["tier3_status"] == "patch_rejected"
    assert "'no_such_function' is not defined" in update["escalation_reason"]
    assert requested == []


def test_a_patch_that_only_changes_some_other_function_is_rejected_for_not_touching_the_target(monkeypatch):
    elsewhere = _real_app_source().replace("VALUES (%s, %s, 'charged')", "VALUES (%s, %s, 'settled')")
    assert elsewhere != _real_app_source()
    calls = []

    def fake_decide_patch(old_content, target, feedback=None):
        calls.append(feedback)
        return PatchDecision(PatchProposal(new_content=elsewhere, rationale="r", target_function="_find_charge_slow"), 1)

    monkeypatch.setattr(tier3_nodes, "decide_patch", fake_decide_patch)
    monkeypatch.setattr(tier3_nodes, "record_audit", lambda *a, **k: None)

    command = tier3_nodes.patch_generator_node(_defect_state())
    update = _update(command)

    assert command.goto == END
    assert update["tier3_status"] == "patch_rejected"
    assert "does not modify the target function '_find_charge_slow'" in update["escalation_reason"]
    assert "_insert_charge" in update["escalation_reason"]
    assert len(calls) == tier3_nodes.MAX_PATCH_ATTEMPTS


def test_fixing_the_target_while_also_editing_another_function_is_rejected(monkeypatch):
    _sabotage, fixed = _sabotage_and_fix()
    both = fixed.replace("VALUES (%s, %s, 'charged')", "VALUES (%s, %s, 'settled')")
    assert both != fixed
    monkeypatch.setattr(
        tier3_nodes, "decide_patch",
        lambda *a: PatchDecision(PatchProposal(new_content=both, rationale="r", target_function="_find_charge_slow"), 1),
    )
    monkeypatch.setattr(tier3_nodes, "record_audit", lambda *a, **k: None)

    command = tier3_nodes.patch_generator_node(_defect_state())
    update = _update(command)

    assert command.goto == END
    assert "outside the target function '_find_charge_slow'" in update["escalation_reason"]
    assert "_insert_charge" in update["escalation_reason"]


def test_declaring_a_different_function_than_the_target_is_rejected_and_retried(monkeypatch):
    _sabotage, fixed = _sabotage_and_fix()
    declared = iter(["find_charge", "_find_charge_slow"])
    feedback_seen = []
    audit_rows = []

    def fake_decide_patch(old_content, target, feedback=None):
        feedback_seen.append(feedback)
        return PatchDecision(PatchProposal(new_content=fixed, rationale="r", target_function=next(declared)), 1)

    monkeypatch.setattr(tier3_nodes, "decide_patch", fake_decide_patch)
    monkeypatch.setattr(tier3_nodes, "record_audit", lambda *a, **k: audit_rows.append(a))

    command = tier3_nodes.patch_generator_node(_defect_state())

    assert command.goto == "patch_validator"
    assert "investigation's target function is '_find_charge_slow'" in feedback_seen[1]
    proposed = [row for row in audit_rows if row[2] == "patch_proposed"]
    assert [row[3]["scope_ok"] for row in proposed] == [False, True]
    assert proposed[0][3]["declared_target_function"] == "find_charge"


def test_the_validator_rechecks_the_committed_diff_against_the_same_target(monkeypatch):
    captured = {}

    def capturing_scope(*a, **k):
        captured.update(k)
        return True, "ok"

    monkeypatch.setattr(tier3_nodes.worktree_tool, "new_branch_name", lambda *a: "phoenix/test-x")
    monkeypatch.setattr(tier3_nodes.worktree_tool, "create_worktree", lambda b: {"status": "ok", "path": "/fake/path", "branch": b})
    monkeypatch.setattr(tier3_nodes.worktree_tool, "write_file_in_worktree", lambda *a: {"status": "ok"})
    monkeypatch.setattr(tier3_nodes.worktree_tool, "commit_patch", lambda *a: {"status": "ok", "commit_sha": "a" * 40})
    monkeypatch.setattr(
        tier3_nodes.worktree_tool, "diff_against_base",
        lambda *a, **k: {"status": "ok", "diff": "@@ -1 +1 @@\n-a\n+b\n", "changed_files": ["services/payment-service/app.py"]},
    )
    monkeypatch.setattr(tier3_nodes.patch_tool, "validate_patch_scope", capturing_scope)
    monkeypatch.setattr(tier3_nodes.test_runner_tool, "discover_tests_for", lambda *a: [])
    monkeypatch.setattr(tier3_nodes.test_runner_tool, "run_tests", lambda *a: {"status": "failed", "returncode": 1})
    monkeypatch.setattr(tier3_nodes.test_runner_tool, "run_linter", lambda *a: {"status": "ok", "returncode": 0})
    monkeypatch.setattr(tier3_nodes.worktree_tool, "discard_worktree", lambda p, b: None)
    monkeypatch.setattr(tier3_nodes, "record_audit", lambda *a, **k: None)

    tier3_nodes.patch_validator_node(_candidate_state())

    assert captured["target_function"] == "_find_charge_slow"
    assert captured["new_content"] == "x = 1\n"
    assert "def _find_charge_slow" in captured["old_content"]


def test_the_validator_fails_closed_when_the_candidate_carries_no_target(monkeypatch):
    discarded = []
    monkeypatch.setattr(tier3_nodes.worktree_tool, "new_branch_name", lambda *a: "phoenix/test-x")
    monkeypatch.setattr(tier3_nodes.worktree_tool, "create_worktree", lambda b: {"status": "ok", "path": "/fake/path", "branch": b})
    monkeypatch.setattr(tier3_nodes.worktree_tool, "write_file_in_worktree", lambda *a: {"status": "ok"})
    monkeypatch.setattr(tier3_nodes.worktree_tool, "commit_patch", lambda *a: {"status": "ok", "commit_sha": "a" * 40})
    monkeypatch.setattr(
        tier3_nodes.worktree_tool, "diff_against_base",
        lambda *a, **k: {"status": "ok", "diff": "@@ -1 +1 @@\n-a\n+b\n", "changed_files": ["services/payment-service/app.py"]},
    )
    monkeypatch.setattr(tier3_nodes.worktree_tool, "discard_worktree", lambda p, b: discarded.append((p, b)))
    monkeypatch.setattr(tier3_nodes, "record_audit", lambda *a, **k: None)
    state = _candidate_state()
    state.patch_candidate.pop("target_function")

    command = tier3_nodes.patch_validator_node(state)
    update = _update(command)

    assert command.goto == END
    assert update["tier3_status"] == "validation_failed"
    assert "fails scope validation" in update["escalation_reason"]
    assert "no target function" in update["escalation_reason"]
    assert discarded
