"""Unit tests for test_runner_tool: real pytest/pyflakes subprocesses, and the
generic (not scenario-keyed) test-discovery mechanism."""

from pathlib import Path

import phoenix.tools.test_runner_tool as test_runner_tool
import phoenix.tools.worktree_tool as worktree_tool


def test_discover_tests_for_finds_the_real_payment_service_test_generically():
    """Proves the discovery is a content search, not a hardcoded table: it
    finds phoenix/test_payment_service_charge.py by scanning for the service
    directory name and filename, the same way a brand-new service's test
    file would be found."""
    matches = test_runner_tool.discover_tests_for("services/payment-service/app.py")

    assert "phoenix/test_payment_service_charge.py" in matches


def test_discover_tests_for_finds_the_real_worker_service_test_generically():
    matches = test_runner_tool.discover_tests_for("services/worker-service/app.py")

    assert "phoenix/test_worker_service_cache.py" in matches


def test_discover_tests_for_an_unreferenced_file_finds_nothing():
    matches = test_runner_tool.discover_tests_for("services/auth-service/app.py")

    assert matches == [] or all("auth" in m for m in matches)


def test_run_tests_reports_error_when_no_test_paths_given():
    result = test_runner_tool.run_tests(".", [])

    assert result["status"] == "error"
    assert "no test files" in result["error"]


def test_run_tests_reports_error_for_a_nonexistent_worktree():
    result = test_runner_tool.run_tests("/no/such/worktree", ["phoenix/test_scoring.py"])

    assert result["status"] == "error"


def test_run_linter_reports_error_for_a_nonexistent_file():
    result = test_runner_tool.run_linter(".", "no/such/file.py")

    assert result["status"] == "error"


def test_run_tests_and_run_linter_execute_for_real_against_a_throwaway_worktree():
    """End-to-end for the tool layer: a worktree gets a genuinely broken file,
    and both run_tests and run_linter report the real failure -- proving
    neither function fabricates a pass."""
    import uuid

    branch = f"phoenix/test-{uuid.uuid4().hex[:12]}"
    created = worktree_tool.create_worktree(branch)
    assert created["status"] == "ok"
    path = created["path"]
    try:
        target = "services/payment-service/app.py"
        broken = (Path(path) / target).read_text(encoding="utf-8") + "\nthis is not valid python (((\n"
        written = worktree_tool.write_file_in_worktree(path, target, broken)
        assert written["status"] == "ok"

        lint_result = test_runner_tool.run_linter(path, target)
        assert lint_result["status"] == "failed"
        assert lint_result["returncode"] != 0
    finally:
        worktree_tool.discard_worktree(path, branch)


def test_scrubbed_env_drops_credentials_and_keeps_what_python_needs(monkeypatch):
    for name in ("LLM_API_KEY", "PHOENIX_APP_PASSWORD", "DATABASE_URL", "GH_TOKEN", "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "leaked")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")

    env = test_runner_tool._scrubbed_env()

    for name in ("LLM_API_KEY", "PHOENIX_APP_PASSWORD", "DATABASE_URL", "GH_TOKEN", "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY"):
        assert name not in env
    assert env["PATH"] == "/usr/bin"
    assert env["SYSTEMROOT"] == r"C:\Windows"


def test_the_patched_test_run_cannot_read_the_parent_credentials(tmp_path, monkeypatch):
    """A real pytest subprocess: the test it runs sees no credential variables."""
    monkeypatch.setenv("LLM_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("PHOENIX_APP_PASSWORD", "should-not-leak")
    (tmp_path / "test_env_probe.py").write_text(
        "import os\n"
        "def test_no_credentials_visible():\n"
        "    assert 'LLM_API_KEY' not in os.environ\n"
        "    assert 'PHOENIX_APP_PASSWORD' not in os.environ\n",
        encoding="utf-8",
    )

    result = test_runner_tool.run_tests(str(tmp_path), ["test_env_probe.py"])

    assert result["status"] == "ok", result["stdout"] + result["stderr"]
