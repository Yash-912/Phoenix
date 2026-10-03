"""Unit tests for worktree_tool: real git worktrees against this checkout,
each one created and discarded within its own test so none leak into the
primary working tree. push_branch's success path (a real push to origin) is
exercised only by the Tier 3 E2E tests, not here -- a unit test must not push
a throwaway branch to the real remote on every run.
"""

import uuid
from pathlib import Path

import pytest

import phoenix.tools.worktree_tool as worktree_tool


def _throwaway_branch() -> str:
    return f"phoenix/test-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def worktree():
    branch = _throwaway_branch()
    created = worktree_tool.create_worktree(branch)
    assert created["status"] == "ok", created
    yield created["path"], branch
    worktree_tool.discard_worktree(created["path"], branch)


def test_create_worktree_rejects_an_unsafe_branch_name():
    result = worktree_tool.create_worktree("not a valid branch; rm -rf /")

    assert result["status"] == "error"


def test_create_worktree_rejects_a_protected_branch_name():
    result = worktree_tool.create_worktree("main")

    assert result["status"] == "error"


def test_push_branch_rejects_a_protected_branch_name():
    result = worktree_tool.push_branch(".", "main")

    assert result["status"] == "error"


def test_create_worktree_actually_creates_a_real_checkout(worktree):
    path, branch = worktree

    assert (Path(path) / "phoenix").is_dir()
    assert (Path(path) / "services" / "payment-service" / "app.py").is_file()


def test_write_file_in_worktree_rejects_a_path_that_escapes_it(worktree):
    path, _branch = worktree

    result = worktree_tool.write_file_in_worktree(path, "../outside.txt", "x")

    assert result["status"] == "error"
    assert "escapes" in result["error"]


def test_write_file_in_worktree_rejects_a_nonexistent_target(worktree):
    path, _branch = worktree

    result = worktree_tool.write_file_in_worktree(path, "does/not/exist.py", "x")

    assert result["status"] == "error"


def test_write_commit_and_diff_round_trip_through_a_real_worktree(worktree):
    path, _branch = worktree
    target = "services/payment-service/app.py"

    original = (Path(path) / target).read_text(encoding="utf-8")
    new_content = original.replace("SLOW_QUERY = False", "SLOW_QUERY = False  # unit-test marker")

    written = worktree_tool.write_file_in_worktree(path, target, new_content)
    assert written["status"] == "ok"

    committed = worktree_tool.commit_patch(path, target, "test: unit-test marker")
    assert committed["status"] == "ok"
    assert len(committed["commit_sha"]) == 40

    diff = worktree_tool.diff_against_base(path, base="HEAD~1")
    assert diff["status"] == "ok"
    assert diff["changed_files"] == [target]
    assert "unit-test marker" in diff["diff"]


def test_discard_worktree_actually_removes_the_checkout():
    branch = _throwaway_branch()
    created = worktree_tool.create_worktree(branch)
    assert created["status"] == "ok"

    assert Path(created["path"]).exists()

    worktree_tool.discard_worktree(created["path"], branch)

    assert not Path(created["path"]).exists()
