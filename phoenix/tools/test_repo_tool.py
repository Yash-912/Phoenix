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


# --- where a service's code lives ---------------------------------------------------------


def test_a_service_whose_directory_exists_resolves_to_it():
    assert repo_tool.service_source_dir("payment-service") == "services/payment-service"


def test_a_service_with_no_directory_resolves_to_nothing():
    assert repo_tool.service_source_dir("no-such-service") is None


def test_a_service_name_that_could_escape_or_nest_is_never_resolved():
    for name in ("", "..", "../phoenix", "services/payment-service", "payment-service/..", "/etc", "a b"):
        assert repo_tool.service_source_dir(name) is None, name


def test_the_context_names_the_service_directory_and_lists_its_source_files():
    ctx = repo_tool.repo_context("payment-service")

    assert ctx["service_dir"] == "services/payment-service"
    assert "services/payment-service/app.py" in ctx["service_files"]
    assert not any("__pycache__" in f for f in ctx["service_files"])


def test_the_context_shows_the_top_level_layout_without_excluded_or_hidden_entries():
    top = repo_tool.repo_context("payment-service")["top_level"]

    assert "services/" in top and "phoenix/" in top
    assert "agentic/" not in top and ".git/" not in top and "__pycache__/" not in top


def test_a_service_with_no_directory_still_gets_the_layout():
    ctx = repo_tool.repo_context("no-such-service")

    assert ctx["service_dir"] is None
    assert ctx["service_files"] == []
    assert "services/" in ctx["top_level"]


def test_a_long_file_list_is_capped_and_says_so(monkeypatch):
    monkeypatch.setattr(repo_tool, "MAX_LAYOUT_FILES", 1)

    ctx = repo_tool.repo_context("payment-service")

    assert len(ctx["service_files"]) == 1
    assert ctx["service_files_truncated"] is True


# --- a wrong path says what the right one probably is -------------------------------------


def test_a_path_with_underscores_for_hyphens_is_pointed_at_the_real_directory():
    result = repo_tool.search_repository("slow", path="payment_service")

    assert result["status"] == "error"
    assert "does not exist" in result["error"]
    assert "services/payment-service" in result["error"]


def test_a_shortened_service_name_is_pointed_at_the_real_directory():
    result = repo_tool.search_repository("leak", path="worker")

    assert result["status"] == "error"
    assert "services/worker-service" in result["error"]


def test_a_misspelled_directory_inside_a_real_one_keeps_the_rest_of_the_path():
    result = repo_tool.read_file("services/payment_service/app.py")

    assert result["status"] == "error"
    assert "services/payment-service/app.py" in result["error"]


def test_a_path_with_no_plausible_match_gets_no_suggestion():
    result = repo_tool.read_file("zzzzqqqq/nothing.py")

    assert result["status"] == "error"
    assert "does not exist" in result["error"]
    assert "did you mean" not in result["error"].lower()


def test_a_suggestion_is_always_a_path_that_exists():
    for guess in ("payment_service", "worker", "payment", "services/payment_service/app.py"):
        for suggested in repo_tool.suggest_paths(guess):
            assert (repo_tool.REPO_ROOT / suggested).exists(), (guess, suggested)
