"""Actually running tests and a linter against a patched worktree.

Neither function here simulates a result. Both shell out -- pytest for
run_tests, pyflakes for run_linter -- and report exactly the subprocess's own
exit code and output. A patch_validator that called these and still reported
success on a nonzero exit would be the "fabricated success" the PRD explicitly
forbids, so the only thing this module decides is which real command to run;
whether that run passed is the subprocess's own verdict, read back verbatim.

discover_tests_for finds the test file(s) covering a given application file by
scanning phoenix/test_*.py for a reference to the same service directory and
filename -- a generic content search, not a scenario -> test-file table. It
works for any new service's test file written the same way the existing ones
are, and knows nothing about Scenario 2 or Scenario 3 specifically.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_DIR = REPO_ROOT / "phoenix"

TEST_TIMEOUT_SECONDS = 120
LINT_TIMEOUT_SECONDS = 30
MAX_OUTPUT_CHARS = 4000


def discover_tests_for(file_path: str) -> list[str]:
    """Repo-relative paths of phoenix/test_*.py files that reference the
    directory and filename of `file_path`, by scanning their source text.

    Both the parent directory name (e.g. "payment-service") and the filename
    (e.g. "app.py") must appear in a candidate test file's text, which is
    what the existing tests already do via APP_PATH -- see
    phoenix/test_payment_service_charge.py line 23. A file with no test
    referencing it returns an empty list, which the validator must treat as
    "nothing could be verified," never as "nothing to verify."
    """
    target = PurePosixPath(file_path.replace("\\", "/"))
    if len(target.parts) < 2:
        return []
    needles = (target.parts[-2], target.name)

    matches: list[str] = []
    if not TEST_DIR.is_dir():
        return matches
    for test_file in sorted(TEST_DIR.glob("test_*.py")):
        try:
            text = test_file.read_text(encoding="utf-8")
        except OSError:
            continue
        if all(needle in text for needle in needles):
            matches.append(str(test_file.relative_to(REPO_ROOT)).replace("\\", "/"))
    return matches


def run_tests(worktree_path: str, test_paths: list[str]) -> dict:
    """Run pytest, for real, against `test_paths` with cwd set to the
    worktree -- so it exercises the patched file, not the primary checkout.
    """
    if not test_paths:
        return {
            "status": "error",
            "error": "no test files were identified for the patched file; nothing was run",
        }

    worktree = Path(worktree_path).resolve()
    if not worktree.is_dir():
        return {"status": "error", "error": f"worktree '{worktree_path}' does not exist"}

    argv = [sys.executable, "-m", "pytest", "-q", *test_paths]
    try:
        result = subprocess.run(
            argv, cwd=str(worktree), capture_output=True, text=True,
            timeout=TEST_TIMEOUT_SECONDS, shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}

    return {
        "status": "ok" if result.returncode == 0 else "failed",
        "returncode": result.returncode,
        "test_paths": test_paths,
        "stdout": result.stdout[-MAX_OUTPUT_CHARS:],
        "stderr": result.stderr[-MAX_OUTPUT_CHARS:],
    }


def run_linter(worktree_path: str, file_path: str) -> dict:
    """Run pyflakes, for real, against the one patched file.

    pyflakes rather than a heavier linter because it is dependency-free static
    analysis (undefined names, unused imports, syntax errors) that needs no
    project-specific config to run correctly against a single file -- which is
    what makes it a check this module can run deterministically on any
    service's file, not a configuration this PRD would have to hardcode per
    service.
    """
    worktree = Path(worktree_path).resolve()
    target = worktree / file_path
    if not target.is_file():
        return {"status": "error", "error": f"'{file_path}' does not exist in the worktree"}

    argv = [sys.executable, "-m", "pyflakes", file_path]
    try:
        result = subprocess.run(
            argv, cwd=str(worktree), capture_output=True, text=True,
            timeout=LINT_TIMEOUT_SECONDS, shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}

    # pyflakes: 0 = no warnings, 1 = warnings found, 2 = it couldn't run at all.
    if result.returncode not in (0, 1):
        return {"status": "error", "error": result.stderr.strip() or "pyflakes could not run"}

    return {
        "status": "ok" if result.returncode == 0 else "failed",
        "returncode": result.returncode,
        "file_path": file_path,
        "stdout": result.stdout[-MAX_OUTPUT_CHARS:],
        "stderr": result.stderr[-MAX_OUTPUT_CHARS:],
    }
