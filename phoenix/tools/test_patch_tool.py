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


# ---- nothing outside the target may change, except what the fix itself needs added

MODULE = (
    '"""Module docs that wrap\n'
    'across two lines."""\n'
    "\n"
    "import os\n"
    "\n"
    "CAP = 100\n"
    "\n"
    "\n"
    "class Store:\n"
    '    """A store."""\n'
    "    limit = 5\n"
    "\n"
    "    def put(self, k):\n"
    "        return k\n"
    "\n"
    "\n"
    "def helper(x):\n"
    "    return x\n"
    "\n"
    "\n"
    "def target(x):\n"
    "    # keep this comment\n"
    "    return helper(x)\n"
)
FIXED = MODULE.replace("    return helper(x)\n", "    return helper(x) + 1\n")


def _outside(new: str, target: str = "target"):
    assert new != MODULE
    return _scope(PAYMENT_APP, MODULE, new, target=target)


def test_a_fix_confined_to_the_target_function_is_accepted():
    ok, reason = _outside(FIXED)

    assert ok is True, reason


def test_a_change_to_the_targets_own_comment_and_body_is_accepted():
    new = MODULE.replace("    # keep this comment\n    return helper(x)\n", "    # reworded\n    return helper(x) * 2\n")

    ok, reason = _outside(new)

    assert ok is True, reason


def test_a_fix_inside_a_method_leaves_its_class_alone_and_is_accepted():
    new = MODULE.replace("        return k\n", "        return k + 1\n", 1)

    ok, reason = _outside(new, target="Store.put")

    assert ok is True, reason


def test_an_import_the_target_uses_may_be_added():
    new = FIXED.replace("import os\n", "import os\nimport functools\n").replace(
        "return helper(x) + 1", "return functools.reduce(max, [helper(x)])"
    )

    ok, reason = _outside(new)

    assert ok is True, reason


def test_a_constant_the_target_uses_may_be_added():
    new = FIXED.replace("CAP = 100\n", "CAP = 100\nLIMIT = 10\n").replace("helper(x) + 1", "helper(x) + LIMIT")

    ok, reason = _outside(new)

    assert ok is True, reason


def test_a_helper_the_target_calls_may_be_added_with_its_blank_lines():
    new = MODULE.replace("return helper(x)\n", "return _indexed(x)\n") + "\n\ndef _indexed(x):\n    return helper(x)\n"

    ok, reason = _outside(new)

    assert ok is True, reason


def test_a_helper_may_be_added_before_the_target_too():
    new = MODULE.replace("    return helper(x)\n", "    return _indexed(x)\n").replace(
        "def target(x):", "def _indexed(x):\n    return helper(x)\n\n\ndef target(x):"
    )
    # only the target's own return line changes among the old lines
    assert new.count("return _indexed(x)") == 1

    ok, reason = _outside(new)

    assert ok is True, reason


def test_rewrapping_the_module_docstring_is_rejected_even_alongside_a_correct_fix():
    new = FIXED.replace('"""Module docs that wrap\nacross two lines."""', '"""Module docs that wrap across two lines."""')

    ok, reason = _outside(new)

    assert ok is False
    assert "outside the target function 'target'" in reason
    assert "module docstring" in reason
    assert "Module docs that wrap" in reason


def test_a_whitespace_or_comment_only_change_elsewhere_is_rejected():
    for new in (
        FIXED.replace("import os\n", "import os  # needed\n"),
        FIXED.replace("import os\n\nCAP", "import os\n\n\nCAP"),
        FIXED.replace("CAP = 100\n", "CAP = 100   \n"),
    ):
        ok, reason = _outside(new)

        assert ok is False, new
        assert "outside the target function 'target'" in reason


def test_an_unrelated_constant_change_is_rejected():
    ok, reason = _outside(FIXED.replace("CAP = 100", "CAP = 200"))

    assert ok is False
    assert "CAP = 200" in reason


def test_an_unrelated_import_removal_is_rejected():
    ok, reason = _outside(FIXED.replace("import os\n\n", ""))

    assert ok is False
    assert "outside the target function 'target'" in reason


def test_an_unrelated_class_attribute_or_docstring_change_is_rejected():
    for new in (FIXED.replace("limit = 5", "limit = 6"), FIXED.replace('"""A store."""', '"""A cache."""')):
        ok, reason = _outside(new)

        assert ok is False, new
        assert "outside the target function 'target'" in reason


def test_an_unrelated_function_change_is_still_rejected_alongside_a_correct_fix():
    ok, reason = _outside(FIXED.replace("def helper(x):\n    return x\n", "def helper(x):\n    return x + 0\n"))

    assert ok is False
    assert "helper" in reason


def test_a_decoy_import_constant_or_helper_the_target_never_uses_is_rejected():
    for added, expected in (
        ("import json\n", "json"),
        ("UNUSED = 1\n", "UNUSED"),
        ("def _decoy():\n    return 1\n", "_decoy"),
    ):
        new = FIXED.replace("CAP = 100\n", "CAP = 100\n" + added)

        ok, reason = _outside(new)

        assert ok is False, added
        assert expected in reason
        assert "does not use" in reason


def test_a_new_class_or_arbitrary_module_code_is_not_a_supporting_declaration():
    for added in ("class Extra:\n    pass\n", "print('loaded')\n"):
        new = FIXED.replace("CAP = 100\n", "CAP = 100\n" + added)

        ok, reason = _outside(new)

        assert ok is False, added
        assert "outside the target function 'target'" in reason


def test_an_addition_used_only_by_another_addition_must_still_be_reachable_from_the_target():
    new = FIXED.replace("CAP = 100\n", "CAP = 100\nBASE = 1\nDERIVED = BASE + 1\n")

    ok, reason = _outside(new)

    assert ok is False
    assert "does not use" in reason


def _scope_real(path: str, old: str, new: str) -> tuple[bool, str]:
    return _scope(path, old, new, target="_cache_store_unbounded")


def test_the_module_docstring_rewrap_from_a_real_agent_patch_is_now_rejected():
    """The shape of the Scenario 3 patch that passed the old gate: the right
    eviction fix inside the target, plus a paragraph of the module docstring
    re-wrapped. The fix alone is accepted; with the re-wrap it is not."""
    path = "services/worker-service/app.py"
    old = patch_tool.read_committed(path)["content"]
    unbounded = "    with _cache_lock:\n        _cache[job_id] = result\n"
    functional = old.replace(
        unbounded,
        "    with _cache_lock:\n"
        "        if job_id not in _cache and len(_cache) >= _CACHE_MAX_SIZE:\n"
        "            oldest_job_id = next(iter(_cache))\n"
        "            del _cache[oldest_job_id]\n"
        "        _cache[job_id] = result\n",
        1,
    )
    paragraph = "the leak could never grow on its own the way a real production queue\nconsumer's cache would."
    rewrapped = functional.replace(paragraph, paragraph.replace("queue\nconsumer", "queue consumer"), 1)
    assert functional != old and rewrapped != functional

    accepted, why = _scope_real(path, old, functional)
    rejected, reason = _scope_real(path, old, rewrapped)

    assert accepted is True, why
    assert rejected is False
    assert "outside the target function '_cache_store_unbounded'" in reason
    assert "module docstring" in reason
