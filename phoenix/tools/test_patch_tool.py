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


def _scope(path: str, old: str, new: str, target: str = "f") -> tuple[bool, str]:
    diff, changed, hunks = _diff(old, new)
    return patch_tool.validate_patch_scope(
        path, diff, changed, hunks, target_function=target, old_content=old, new_content=new
    )


# A small file shaped like any "dispatcher selects between a good and a
# defective implementation" module. Nothing here is specific to one scenario.
DISPATCH_OLD = (
    "def find(x):\n"
    "    return slow(x) if use_slow else fast(x)\n"
    "\n"
    "def fast(x):\n"
    "    return lookup(x)\n"
    "\n"
    "def slow(x):\n"
    "    return scan_all(x)\n"
)


def test_validate_patch_scope_rejects_a_test_file():
    ok, reason = _scope("phoenix/test_payment_service_charge.py", "a\n", "b\n")

    assert ok is False
    assert "test file" in reason


def test_validate_patch_scope_rejects_a_lock_file():
    ok, reason = _scope("services/payment-service/requirements.txt", "a\n", "b\n")

    assert ok is False
    assert "lock file" in reason


def test_validate_patch_scope_rejects_a_chaos_toggle_change():
    ok, reason = _scope(PAYMENT_APP, "SLOW_QUERY = True\n", "SLOW_QUERY = False\n")

    assert ok is False
    assert "chaos toggle" in reason


def test_chaos_toggle_rejection_quotes_the_changed_lines_that_tripped_it():
    old = "def find(x):\n    return slow(x) if SLOW_QUERY else fast(x)\n\ndef slow(x):\n    return x\n"
    new = "def find(x):\n    return fast(x)\n\ndef slow(x):\n    return x\n"
    ok, reason = _scope(PAYMENT_APP, old, new, target="slow")

    assert ok is False
    assert "chaos toggle" in reason
    assert "return slow(x) if SLOW_QUERY else fast(x)" in reason


def test_a_fix_inside_the_defective_function_is_not_rejected_for_sitting_next_to_the_toggle():
    old = "def find(x):\n    return slow(x) if SLOW_QUERY else fast(x)\n\ndef slow(x):\n    return scan_all(x)\n"
    new = "def find(x):\n    return slow(x) if SLOW_QUERY else fast(x)\n\ndef slow(x):\n    return indexed(x)\n"
    ok, reason = _scope(PAYMENT_APP, old, new, target="slow")

    assert ok is True, reason


def test_validate_patch_scope_rejects_a_path_that_escapes_the_repo():
    ok, reason = _scope("../../outside/app.py", "a\n", "b\n")

    assert ok is False
    assert "escapes" in reason


def test_validate_patch_scope_rejects_a_patch_over_the_line_cap():
    old = "\n".join(f"line{i}" for i in range(100)) + "\n"
    new = "\n".join(f"changed{i}" for i in range(100)) + "\n"

    ok, reason = _scope(PAYMENT_APP, old, new)

    assert ok is False
    assert "line" in reason


def test_validate_patch_scope_rejects_a_patch_with_too_many_hunks():
    old = "\n".join(f"line{i}" for i in range(50)) + "\n"
    new_lines = [f"line{i}" if i % 10 != 0 else f"changed{i}" for i in range(50)]
    new = "\n".join(new_lines) + "\n"

    ok, reason = _scope(PAYMENT_APP, old, new)

    assert ok is False
    assert "regions" in reason


def test_validate_patch_scope_rejects_a_no_op_diff():
    ok, reason = patch_tool.validate_patch_scope(
        PAYMENT_APP, "", 0, 0, target_function="f", old_content="x = 1\n", new_content="x = 1\n"
    )

    assert ok is False
    assert "no changes" in reason


def test_validate_patch_scope_accepts_a_minimal_in_scope_fix():
    ok, reason = _scope(PAYMENT_APP, "def f():\n    return 1\n", "def f():\n    return 2\n")

    assert ok is True
    assert "within scope" in reason


# ---- the investigation's target function is the only code a patch may change


def test_a_fix_inside_the_target_function_is_accepted():
    new = DISPATCH_OLD.replace("return scan_all(x)", "return indexed(x)")

    ok, reason = _scope(PAYMENT_APP, DISPATCH_OLD, new, target="slow")

    assert ok is True, reason


def test_a_patch_that_only_rewrites_the_dispatcher_is_rejected_when_another_function_is_the_target():
    new = DISPATCH_OLD.replace("return slow(x) if use_slow else fast(x)", "return fast(x)")

    ok, reason = _scope(PAYMENT_APP, DISPATCH_OLD, new, target="slow")

    assert ok is False
    assert "does not modify the target function 'slow'" in reason
    assert "find" in reason


def test_a_cosmetic_rewrite_of_the_dispatcher_is_still_not_a_fix_to_the_target():
    new = DISPATCH_OLD.replace(
        "    return slow(x) if use_slow else fast(x)\n",
        "    if use_slow:\n        return slow(x)\n    return fast(x)\n",
    )

    ok, reason = _scope(PAYMENT_APP, DISPATCH_OLD, new, target="slow")

    assert ok is False
    assert "does not modify the target function 'slow'" in reason


def test_fixing_the_target_while_also_editing_another_function_is_rejected():
    new = DISPATCH_OLD.replace("return scan_all(x)", "return indexed(x)").replace(
        "return slow(x) if use_slow else fast(x)", "return fast(x)"
    )

    ok, reason = _scope(PAYMENT_APP, DISPATCH_OLD, new, target="slow")

    assert ok is False
    assert "outside the target function 'slow'" in reason
    assert "find" in reason


def test_the_dispatcher_may_be_changed_when_it_is_the_identified_target():
    new = DISPATCH_OLD.replace("return slow(x) if use_slow else fast(x)", "return fast(x)")

    ok, reason = _scope(PAYMENT_APP, DISPATCH_OLD, new, target="find")

    assert ok is True, reason


def test_a_new_import_or_module_constant_may_accompany_the_target_fix():
    old = "def slow(x):\n    return scan_all(x)\n"
    new = "import functools\nLIMIT = 10\n\ndef slow(x):\n    return functools.reduce(max, x[:LIMIT])\n"

    ok, reason = _scope(PAYMENT_APP, old, new, target="slow")

    assert ok is True, reason


def test_a_new_helper_function_may_accompany_the_target_fix():
    new = DISPATCH_OLD.replace("return scan_all(x)", "return _indexed(x)") + "\ndef _indexed(x):\n    return lookup(x)\n"

    ok, reason = _scope(PAYMENT_APP, DISPATCH_OLD, new, target="slow")

    assert ok is True, reason


def test_removing_or_renaming_the_target_function_is_rejected():
    new = DISPATCH_OLD.replace("def slow(x):", "def slower(x):")

    ok, reason = _scope(PAYMENT_APP, DISPATCH_OLD, new, target="slow")

    assert ok is False
    assert "removes or renames the target function 'slow'" in reason


def test_a_target_that_is_not_in_the_file_is_rejected():
    new = DISPATCH_OLD.replace("return scan_all(x)", "return indexed(x)")

    ok, reason = _scope(PAYMENT_APP, DISPATCH_OLD, new, target="missing")

    assert ok is False
    assert "'missing'" in reason and "not defined" in reason


def test_an_empty_target_fails_closed():
    new = DISPATCH_OLD.replace("return scan_all(x)", "return indexed(x)")

    ok, reason = _scope(PAYMENT_APP, DISPATCH_OLD, new, target="  ")

    assert ok is False
    assert "no target function" in reason


def test_a_patch_that_does_not_parse_is_rejected():
    new = DISPATCH_OLD.replace("return scan_all(x)", "return (scan_all(x")

    ok, reason = _scope(PAYMENT_APP, DISPATCH_OLD, new, target="slow")

    assert ok is False
    assert "does not parse" in reason


def test_a_non_python_file_cannot_be_verified_against_a_target_function():
    ok, reason = _scope("services/payment-service/Dockerfile", "FROM a\n", "FROM b\n", target="slow")

    assert ok is False
    assert "Python" in reason


def test_dotted_and_call_style_target_names_are_normalised_to_the_function_name():
    old = "class Store:\n    def put(self, k):\n        return k\n    def get(self, k):\n        return k\n"
    new = old.replace("def put(self, k):\n        return k", "def put(self, k):\n        return k + 1")

    assert _scope(PAYMENT_APP, old, new, target="Store.put")[0] is True
    assert _scope(PAYMENT_APP, old, new, target="put()")[0] is True
    assert _scope(PAYMENT_APP, old, new, target="get")[0] is False


def test_function_exists_finds_functions_and_methods_by_normalised_name():
    content = "def a():\n    pass\n\nclass C:\n    def m(self):\n        pass\n"

    assert patch_tool.function_exists(content, "a") is True
    assert patch_tool.function_exists(content, "C.m") is True
    assert patch_tool.function_exists(content, "m()") is True
    assert patch_tool.function_exists(content, "nope") is False
    assert patch_tool.function_exists("def broken(:\n", "broken") is False


def test_same_target_compares_function_names_not_spellings():
    assert patch_tool.same_target("put", "Store.put") is True
    assert patch_tool.same_target("Store.put()", "put") is True
    assert patch_tool.same_target("get", "put") is False
    assert patch_tool.same_target("", "put") is False
    assert patch_tool.same_target("put", "") is False


def test_forbidden_areas_name_dispatchers_toggles_and_tests_generically():
    joined = " ".join(patch_tool.FORBIDDEN_AREAS).lower()

    assert "dispatcher" in joined
    assert "toggle" in joined
    assert "test" in joined
