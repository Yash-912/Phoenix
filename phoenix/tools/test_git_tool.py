"""Unit tests for git_tool: real git calls against this checkout, plus the
refusal paths that keep a ref/path from ever reaching the subprocess as
something other than a ref/path."""

import phoenix.tools.git_tool as git_tool


def test_get_git_commits_returns_real_history():
    result = git_tool.get_git_commits(limit=3)

    assert result["status"] == "ok"
    assert len(result["commits"]) == 3
    assert all(len(c["sha"]) == 40 for c in result["commits"])


def test_get_git_commits_rejects_a_path_outside_the_repo():
    result = git_tool.get_git_commits(path="../outside")

    assert result["status"] == "error"
    assert "outside" in result["error"]


def test_get_git_commits_scoped_to_a_real_file_only_returns_commits_touching_it():
    result = git_tool.get_git_commits(path="services/payment-service/app.py", limit=5)

    assert result["status"] == "ok"
    assert len(result["commits"]) >= 1


def test_get_git_diff_rejects_a_ref_that_looks_like_a_flag():
    result = git_tool.get_git_diff(base="--upload-pack=touch x")

    assert result["status"] == "error"
    assert "not a valid git ref" in result["error"]


def test_get_git_diff_rejects_a_path_outside_the_repo():
    result = git_tool.get_git_diff(path="../../outside")

    assert result["status"] == "error"
    assert "outside" in result["error"]


def test_get_git_diff_returns_a_real_diff_between_head_and_itself_is_empty():
    result = git_tool.get_git_diff(base="HEAD", head="HEAD")

    assert result["status"] == "ok"
    assert result["diff"] == ""


def test_get_git_diff_on_head_tilde_one_returns_real_unified_diff_markers():
    result = git_tool.get_git_diff(base="HEAD~1", head="HEAD")

    assert result["status"] == "ok"
    assert "diff --git" in result["diff"] or result["diff"] == ""
