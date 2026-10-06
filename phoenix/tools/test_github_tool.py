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


# --- recognising a PR that already carries this change ----------------------------------


def _diff(path="services/payment-service/app.py", removed="    cur.execute(slow)", added="    cur.execute(fast)",
          index="1a2b3c4..5d6e7f8", hunk="@@ -40,7 +40,7 @@ def charge():"):
    return (
        f"diff --git a/{path} b/{path}\nindex {index} 100644\n--- a/{path}\n+++ b/{path}\n"
        f"{hunk}\n context\n-{removed}\n+{added}\n context\n"
    )


def test_the_same_change_on_a_shifted_base_has_the_same_fingerprint():
    shifted = _diff(index="9f9f9f9..0a0a0a0", hunk="@@ -52,7 +52,7 @@ def other():")

    assert github_tool.diff_fingerprint(_diff()) == github_tool.diff_fingerprint(shifted)


def test_trailing_whitespace_and_line_endings_do_not_change_the_fingerprint():
    noisy = _diff().replace("\n", " \r\n")

    assert github_tool.diff_fingerprint(_diff()) == github_tool.diff_fingerprint(noisy)


def test_a_different_changed_line_is_a_different_fingerprint():
    assert github_tool.diff_fingerprint(_diff()) != github_tool.diff_fingerprint(_diff(added="    cur.execute(other)"))


def test_the_same_lines_in_a_different_file_are_a_different_fingerprint():
    assert github_tool.diff_fingerprint(_diff()) != github_tool.diff_fingerprint(_diff(path="services/auth-service/app.py"))


def test_a_diff_with_no_changed_lines_has_no_fingerprint():
    assert github_tool.diff_fingerprint("") is None
    assert github_tool.diff_fingerprint("diff --git a/x b/x\nindex 1..2 100644\n") is None


def _gh(monkeypatch, listing, diffs, calls=None):
    """A fake gh: `pr list` answers `listing` (a JSON string), `pr diff N` answers diffs[N]."""
    import json

    def fake_run(argv, **kwargs):
        if calls is not None:
            calls.append(argv)
        if argv[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(argv, 0, stdout=listing if isinstance(listing, str) else json.dumps(listing), stderr="")
        if argv[:3] == ["gh", "pr", "diff"]:
            value = diffs[int(argv[3])]
            if value is None:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="could not fetch")
            return subprocess.CompletedProcess(argv, 0, stdout=value, stderr="")
        raise AssertionError(f"unexpected gh call {argv}")

    monkeypatch.setattr(subprocess, "run", fake_run)


def _pr(number, title="[Phoenix Tier 3] incident #7: fix services/payment-service/app.py"):
    return {"number": number, "url": f"https://github.com/x/y/pull/{number}", "title": title, "headRefName": f"phoenix/b{number}"}


def test_an_open_phoenix_pr_with_the_same_change_is_reported_as_the_duplicate(monkeypatch):
    _gh(monkeypatch, [_pr(24), _pr(25)], {24: _diff(added="    other"), 25: _diff(index="aaa..bbb")})

    result = github_tool.find_open_duplicate(_diff())

    assert result["status"] == "ok"
    assert result["duplicate"] == {"number": 25, "url": "https://github.com/x/y/pull/25", "branch": "phoenix/b25"}


def test_no_open_pr_with_the_same_change_means_no_duplicate(monkeypatch):
    _gh(monkeypatch, [_pr(24)], {24: _diff(added="    other")})

    assert github_tool.find_open_duplicate(_diff()) == {"status": "ok", "duplicate": None}


def test_a_pr_that_is_not_a_phoenix_pr_is_never_diffed_or_matched(monkeypatch):
    calls = []
    _gh(monkeypatch, [_pr(3, title="someone's own PR")], {3: _diff()}, calls)

    assert github_tool.find_open_duplicate(_diff())["duplicate"] is None
    assert not any(argv[:3] == ["gh", "pr", "diff"] for argv in calls)


def test_the_lookup_asks_only_for_open_prs_and_never_shells_out_with_shell_true(monkeypatch):
    calls, kwargs_seen = [], []
    import json

    def fake_run(argv, **kwargs):
        calls.append(argv)
        kwargs_seen.append(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps([]), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    github_tool.find_open_duplicate(_diff())

    assert calls[0][:3] == ["gh", "pr", "list"] and "open" in calls[0]
    assert all(k["shell"] is False for k in kwargs_seen)


def test_a_pr_whose_diff_cannot_be_read_is_skipped_not_fatal(monkeypatch):
    _gh(monkeypatch, [_pr(24), _pr(25)], {24: None, 25: _diff()})

    assert github_tool.find_open_duplicate(_diff())["duplicate"]["number"] == 25


def test_a_failed_listing_is_an_error_not_an_answer_of_no_duplicate(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, stdout="", stderr="auth required"))

    result = github_tool.find_open_duplicate(_diff())

    assert result["status"] == "error" and "auth required" in result["error"]


def test_an_unreadable_listing_is_an_error(monkeypatch):
    _gh(monkeypatch, "not json", {})

    assert github_tool.find_open_duplicate(_diff())["status"] == "error"


def test_a_diff_with_nothing_to_fingerprint_is_an_error_never_a_match(monkeypatch):
    _gh(monkeypatch, [_pr(24)], {24: ""})

    assert github_tool.find_open_duplicate("")["status"] == "error"


# --- commenting on the PR that already exists --------------------------------------------


def test_comment_on_pull_request_posts_to_that_pr_without_a_shell(monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured.update(argv=argv, kwargs=kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="https://github.com/x/y/pull/25#issuecomment-1\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = github_tool.comment_on_pull_request(25, "seen again in incident 9")

    assert result["status"] == "ok"
    assert captured["argv"][:4] == ["gh", "pr", "comment", "25"]
    assert "seen again in incident 9" in captured["argv"]
    assert captured["kwargs"]["shell"] is False


def test_comment_on_pull_request_refuses_a_non_numeric_pr_and_an_empty_body(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran gh")))

    assert github_tool.comment_on_pull_request("25; rm -rf", "x")["status"] == "error"
    assert github_tool.comment_on_pull_request(25, "  ")["status"] == "error"


def test_comment_on_pull_request_reports_the_real_gh_failure(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, stdout="", stderr="no permission"))

    result = github_tool.comment_on_pull_request(25, "hello")

    assert result["status"] == "error" and "no permission" in result["error"]


def test_open_pull_request_never_fabricates_a_url_when_gh_reports_no_url(monkeypatch):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="no url here\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = github_tool.open_pull_request("phoenix/tier3-test", "A title", "A body")

    assert result["status"] == "error"
    assert "no URL" in result["error"]
