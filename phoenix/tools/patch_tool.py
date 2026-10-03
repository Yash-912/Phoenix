"""Turning a proposed file body into a diff, and deciding whether that diff is
safe to even attempt applying.

Two separate jobs on purpose. generate_patch is pure data transformation: old
content in, new content in, a unified diff out -- it never decides whether the
change is acceptable, only computes what changed. validate_patch_scope is the
deterministic gate the PRD calls for: every rejection reason it can return
(multi-file is structurally impossible here since one call is one file;
a test file; a chaos toggle; a lock file; a rename/delete masquerading as a
content change; too large to be "minimal") is checked here, in code the LLM
never runs, before anything touches a worktree.

Nothing in this module ever writes to REPO_ROOT. generate_patch only reads the
current file to diff against; the write happens later, inside an isolated
worktree, in phoenix.tools.worktree_tool.
"""

from __future__ import annotations

import difflib
import re
import subprocess
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parents[2]

MAX_CHANGED_LINES = 60
MAX_HUNKS = 4

LOCK_FILE_NAMES = {
    "requirements.txt", "package-lock.json", "poetry.lock", "Pipfile.lock",
    "uv.lock", "package.json", "yarn.lock", "Cargo.lock",
}

BINARY_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".pyc", ".so", ".dll", ".exe"}

# Chaos-mechanism identifiers across both real scenarios' services, named
# generically (flags, env toggle, the /chaos/ route prefix, the hard-kill
# call) rather than per-scenario, so the same check rejects a patch that
# "fixes" Scenario 2 by touching SLOW_QUERY and one that "fixes" Scenario 3 by
# touching LEAK_ENABLED -- and any future chaos toggle named the same way --
# without needing a new entry per scenario.
CHAOS_TOKEN_PATTERN = re.compile(
    r"CHAOS_ENABLED|SLOW_QUERY|LEAK_ENABLED|CPU_SPIKE|BLIP_UNTIL|/chaos/|os\._exit"
)


def _resolve_in_repo(rel_path: str) -> Path | None:
    if not rel_path or Path(rel_path).is_absolute():
        return None
    try:
        candidate = (REPO_ROOT / rel_path).resolve()
        candidate.relative_to(REPO_ROOT)
    except (OSError, ValueError):
        return None
    return candidate


def read_committed(file_path: str) -> dict:
    """The content of `file_path` as committed at HEAD, not as it sits in the
    working tree.

    Tier 3 applies its patch inside a worktree created from HEAD, so the diff
    has to be computed against HEAD too. Diffing against the working tree
    would drag any uncommitted local edit into the "actual" committed diff
    (and into the PR), which is both wrong and a way for unreviewed local
    changes to ride along.
    """
    resolved = _resolve_in_repo(file_path)
    if resolved is None:
        return {"status": "error", "error": f"'{file_path}' is outside the repository"}
    rel = str(resolved.relative_to(REPO_ROOT)).replace("\\", "/")
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "show", f"HEAD:{rel}"],
            capture_output=True, text=True, encoding="utf-8", timeout=20, shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    if result.returncode != 0:
        return {"status": "error", "error": f"'{rel}' is not tracked at HEAD: {result.stderr.strip()}"}
    return {"status": "ok", "path": rel, "content": result.stdout}


def generate_patch(file_path: str, new_content: str) -> dict:
    """Diff `new_content` against the file currently on disk at `file_path`.

    Refuses (as an error envelope, never raising) a path outside the repo, a
    path that doesn't already exist (Tier 3 modifies one existing file, never
    creates one), a binary file, or content identical to what's already
    there. Everything else about whether the change is *acceptable* is
    validate_patch_scope's job, not this function's.
    """
    resolved = _resolve_in_repo(file_path)
    if resolved is None:
        return {"status": "error", "error": f"'{file_path}' is outside the repository"}
    if not resolved.exists() or not resolved.is_file():
        return {"status": "error", "error": f"'{file_path}' does not exist as a file in the repository"}
    if resolved.suffix.lower() in BINARY_EXTENSIONS:
        return {"status": "error", "error": f"'{file_path}' is a binary file"}

    committed = read_committed(file_path)
    if committed.get("status") != "ok":
        return committed
    old_content = committed["content"]

    if not isinstance(new_content, str) or not new_content.strip():
        return {"status": "error", "error": "new_content must be non-empty text"}
    if new_content == old_content:
        return {"status": "error", "error": "new_content is identical to the current file; nothing to patch"}

    rel = str(resolved.relative_to(REPO_ROOT)).replace("\\", "/")
    diff_lines = list(difflib.unified_diff(
        old_content.splitlines(keepends=True),
        new_content.splitlines(keepends=True),
        fromfile=f"a/{rel}",
        tofile=f"b/{rel}",
    ))
    diff_text = "".join(diff_lines)
    changed = [l for l in diff_lines if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))]
    hunks = sum(1 for l in diff_lines if l.startswith("@@"))

    return {
        "status": "ok",
        "file_path": rel,
        "old_content": old_content,
        "new_content": new_content,
        "diff": diff_text,
        "changed_lines": len(changed),
        "hunks": hunks,
    }


def validate_patch_scope(file_path: str, diff_text: str, changed_lines: int, hunks: int) -> tuple[bool, str]:
    """The deterministic gate. Returns (ok, reason) -- reason is always
    filled, whether the verdict is a pass or a specific rejection, so the
    audit trail never records a bare True/False.
    """
    rel = PurePosixPath(file_path.replace("\\", "/"))
    name = rel.name
    parts = rel.parts

    if ".." in parts or rel.is_absolute():
        return False, f"patch path '{file_path}' escapes the repository"
    if name.startswith("test_") or name.endswith("_test.py") or "tests" in parts:
        return False, f"patch touches a test file ('{file_path}'), which Tier 3 must never modify"
    if name in LOCK_FILE_NAMES or name.endswith(".lock"):
        return False, f"patch touches a dependency/lock file ('{file_path}')"
    if Path(file_path).suffix.lower() in BINARY_EXTENSIONS:
        return False, f"patch touches a binary file ('{file_path}')"
    # Scanned against only the +/- changed lines, never the surrounding
    # unchanged context difflib includes by default: a real fix to
    # _find_charge_slow sits lines away from the SLOW_QUERY toggle it is
    # dispatched by, and that neighbour would land in the same 3-line context
    # window. Checking the whole diff_text would reject the real Scenario 2
    # fix for being textually near a toggle it never touches.
    changed_text = "\n".join(
        line for line in diff_text.splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    )
    if CHAOS_TOKEN_PATTERN.search(changed_text):
        return False, "patch modifies a chaos toggle/flag rather than the application defect"
    if changed_lines == 0:
        return False, "patch contains no changes"
    if changed_lines > MAX_CHANGED_LINES:
        return False, (
            f"patch changes {changed_lines} lines, over the {MAX_CHANGED_LINES}-line "
            f"minimal-patch cap for a single-file Tier 3 fix"
        )
    if hunks > MAX_HUNKS:
        return False, (
            f"patch touches {hunks} separate regions of the file, over the {MAX_HUNKS}-hunk "
            f"cap for a minimal, targeted fix"
        )

    return True, f"patch to '{file_path}' changes {changed_lines} lines across {hunks} hunk(s), within scope"
