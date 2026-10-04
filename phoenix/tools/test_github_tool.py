"""Unit tests for github_tool: the refusal paths, and the argv/parsing
contract against a monkeypatched subprocess -- never a real `gh pr create`.
The real call is exercised only by the Tier 3 E2E integration tests.
"""

import subprocess

import phoenix.tools.github_tool as github_tool


def test_open_pull_request_refuses_a_protected_head_branch():
    result = github_tool.open_pull_request("main", "title", "body")

    assert result["status"] == "error"
    assert "protected" in result["error"]


def test_open_pull_request_refuses_an_empty_title():
    result = github_tool.open_pull_request("phoenix/some-branch", "   ", "body")

    assert result["status"] == "error"


def test_open_pull_request_never_shells_out_with_shell_true(monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, stdout="https://github.com/x/y/pull/1\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = github_tool.open_pull_request("phoenix/tier3-test", "A title", "A body")

    assert result["status"] == "ok"
    assert result["url"] == "https://github.com/x/y/pull/1"
    assert captured["kwargs"]["shell"] is False
    assert captured["argv"][:3] == ["gh", "pr", "create"]
    assert "--head" in captured["argv"] and "phoenix/tier3-test" in captured["argv"]


def test_open_pull_request_reports_the_real_gh_failure(monkeypatch):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="pull request create failed: some reason")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = github_tool.open_pull_request("phoenix/tier3-test", "A title", "A body")

    assert result["status"] == "error"
    assert "some reason" in result["error"]


def test_open_pull_request_never_fabricates_a_url_when_gh_reports_no_url(monkeypatch):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="no url here\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = github_tool.open_pull_request("phoenix/tier3-test", "A title", "A body")

    assert result["status"] == "error"
    assert "no URL" in result["error"]
