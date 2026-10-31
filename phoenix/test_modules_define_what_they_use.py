"""No test module in the repository uses a name it never imported or defined.

phoenix/graph/test_graph.py called pytest.fail in seven places without importing pytest.
Those lines only run when the code under test misbehaves, so the NameError that would
have reported the failure replaced it with a different, misleading one. A failing guard
has to fail as itself, so every undefined name in a test module is a failure here.
"""

import ast
import pathlib

import pytest

pyflakes_checker = pytest.importorskip("pyflakes.checker")
pyflakes_messages = pytest.importorskip("pyflakes.messages")

ROOT = pathlib.Path(__file__).resolve().parents[1]
SKIPPED_PARTS = {"agentic", ".git", "__pycache__", ".phoenix_worktrees", "node_modules", ".venv", "venv"}


def _test_files():
    for pattern in ("test_*.py", "conftest.py"):
        for path in ROOT.rglob(pattern):
            if not SKIPPED_PARTS.intersection(path.relative_to(ROOT).parts):
                yield path


def test_every_test_module_defines_the_names_it_uses():
    undefined = []
    for path in _test_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for message in pyflakes_checker.Checker(tree, filename=str(path)).messages:
            if isinstance(message, pyflakes_messages.UndefinedName):
                undefined.append(f"{path.relative_to(ROOT)}:{message.lineno} {message.message_args[0]}")

    assert undefined == []
