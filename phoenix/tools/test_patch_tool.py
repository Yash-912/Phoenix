"""Unit tests for the deterministic patch gate: every rejection reason the
PRD names, proven against real file content rather than mocked structures."""

import phoenix.tools.patch_tool as patch_tool

PAYMENT_APP = "services/payment-service/app.py"


def test_generate_patch_rejects_a_path_outside_the_repo():
    result = patch_tool.generate_patch("../../etc/passwd", "x = 1\n")

    assert result["status"] == "error"
    assert "outside" in result["error"]


def test_generate_patch_rejects_a_nonexistent_file():
    result = patch_tool.generate_patch("services/payment-service/does_not_exist.py", "x = 1\n")

    assert result["status"] == "error"
    assert "does not exist" in result["error"]


def test_generate_patch_rejects_identical_content():
    current = patch_tool.read_committed(PAYMENT_APP)["content"]

    result = patch_tool.generate_patch(PAYMENT_APP, current)

    assert result["status"] == "error"
    assert "identical" in result["error"]


def test_generate_patch_produces_a_real_diff_for_a_real_change():
    current = patch_tool.read_committed(PAYMENT_APP)["content"]
    new_content = current.replace("SLOW_QUERY = False", "SLOW_QUERY = False  # tweak")

    result = patch_tool.generate_patch(PAYMENT_APP, new_content)

    assert result["status"] == "ok"
    assert result["file_path"] == PAYMENT_APP
    assert "@@" in result["diff"]
    assert result["changed_lines"] >= 1


def _diff(old: str, new: str) -> tuple[str, int, int]:
    import difflib
    lines = list(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True)))
    text = "".join(lines)
    changed = [l for l in lines if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))]
    hunks = sum(1 for l in lines if l.startswith("@@"))
    return text, len(changed), hunks


def test_validate_patch_scope_rejects_a_test_file():
    diff, changed, hunks = _diff("a\n", "b\n")
    ok, reason = patch_tool.validate_patch_scope("phoenix/test_payment_service_charge.py", diff, changed, hunks)

    assert ok is False
    assert "test file" in reason


def test_validate_patch_scope_rejects_a_lock_file():
    diff, changed, hunks = _diff("a\n", "b\n")
    ok, reason = patch_tool.validate_patch_scope("services/payment-service/requirements.txt", diff, changed, hunks)

    assert ok is False
    assert "lock file" in reason


def test_validate_patch_scope_rejects_a_chaos_toggle_change():
    diff, changed, hunks = _diff("SLOW_QUERY = True\n", "SLOW_QUERY = False\n")
    ok, reason = patch_tool.validate_patch_scope("services/payment-service/app.py", diff, changed, hunks)

    assert ok is False
    assert "chaos toggle" in reason


def test_validate_patch_scope_rejects_a_path_that_escapes_the_repo():
    diff, changed, hunks = _diff("a\n", "b\n")
    ok, reason = patch_tool.validate_patch_scope("../../outside/app.py", diff, changed, hunks)

    assert ok is False
    assert "escapes" in reason


def test_validate_patch_scope_rejects_a_patch_over_the_line_cap():
    old = "\n".join(f"line{i}" for i in range(100)) + "\n"
    new = "\n".join(f"changed{i}" for i in range(100)) + "\n"
    diff, changed, hunks = _diff(old, new)

    ok, reason = patch_tool.validate_patch_scope("services/payment-service/app.py", diff, changed, hunks)

    assert ok is False
    assert "line" in reason


def test_validate_patch_scope_rejects_a_patch_with_too_many_hunks():
    old = "\n".join(f"line{i}" for i in range(50)) + "\n"
    new_lines = [f"line{i}" if i % 10 != 0 else f"changed{i}" for i in range(50)]
    new = "\n".join(new_lines) + "\n"
    diff, changed, hunks = _diff(old, new)

    ok, reason = patch_tool.validate_patch_scope("services/payment-service/app.py", diff, changed, hunks)

    assert ok is False
    assert "regions" in reason


def test_validate_patch_scope_rejects_a_no_op_diff():
    ok, reason = patch_tool.validate_patch_scope("services/payment-service/app.py", "", 0, 0)

    assert ok is False
    assert "no changes" in reason


def test_validate_patch_scope_accepts_a_minimal_in_scope_fix():
    diff, changed, hunks = _diff(
        "def f():\n    return 1\n",
        "def f():\n    return 2\n",
    )
    ok, reason = patch_tool.validate_patch_scope("services/payment-service/app.py", diff, changed, hunks)

    assert ok is True
    assert "within scope" in reason
