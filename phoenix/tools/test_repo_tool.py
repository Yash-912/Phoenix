"""Unit tests for repo_tool's safety boundary: everything stays under
REPO_ROOT, nothing escapes via traversal or an absolute path."""

import phoenix.tools.repo_tool as repo_tool


def test_read_file_rejects_path_traversal_out_of_the_repo():
    result = repo_tool.read_file("../../../../etc/passwd")

    assert result["status"] == "error"
    assert "outside" in result["error"]


def test_read_file_rejects_an_absolute_path():
    result = repo_tool.read_file("/etc/passwd")

    assert result["status"] == "error"


def test_read_file_rejects_a_nonexistent_file():
    result = repo_tool.read_file("phoenix/this_file_does_not_exist.py")

    assert result["status"] == "error"
    assert "does not exist" in result["error"]


def test_read_file_reads_a_real_file_in_the_repo():
    result = repo_tool.read_file("phoenix/tools/repo_tool.py")

    assert result["status"] == "ok"
    assert "def read_file" in result["content"]
    assert result["path"] == "phoenix/tools/repo_tool.py"


def test_read_file_honors_a_line_range():
    result = repo_tool.read_file("phoenix/tools/repo_tool.py", start_line=1, end_line=3)

    assert result["status"] == "ok"
    assert result["start_line"] == 1
    assert result["end_line"] == 3
    assert len(result["content"].splitlines()) == 3


def test_search_repository_rejects_a_path_outside_the_repo():
    result = repo_tool.search_repository("def", path="../outside")

    assert result["status"] == "error"
    assert "outside" in result["error"]


def test_search_repository_rejects_an_empty_query():
    result = repo_tool.search_repository("   ")

    assert result["status"] == "error"


def test_search_repository_finds_a_real_known_string():
    result = repo_tool.search_repository("_find_charge_slow", path="services/payment-service")

    assert result["status"] == "ok"
    files = {m["file"] for m in result["matches"]}
    assert "services/payment-service/app.py" in files


def test_search_repository_never_returns_a_match_outside_the_scoped_path():
    result = repo_tool.search_repository("import", path="phoenix/tools")

    assert result["status"] == "ok"
    assert all(m["file"].startswith("phoenix/tools/") for m in result["matches"])
