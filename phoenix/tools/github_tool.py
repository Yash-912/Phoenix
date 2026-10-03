"""Opening a real pull request through the authenticated `gh` CLI -- and
nothing past that.

This module exposes exactly one capability: open_pull_request. There is no
merge_pull_request, no approve_pull_request, no function anywhere in this
file that can push to main/master or change branch protection -- the
structural absence is the safety boundary, not a flag this module checks and
could get wrong. worktree_tool.PROTECTED_BRANCHES is re-checked here too, so
this module refuses on its own even if a caller's guard is ever bypassed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from phoenix.tools.worktree_tool import PROTECTED_BRANCHES

REPO_ROOT = Path(__file__).resolve().parents[2]
GH_TIMEOUT_SECONDS = 30


def open_pull_request(branch: str, title: str, body: str, base: str = "main") -> dict:
    """`gh pr create --head <branch> --base <base> ...` against the real repo.

    Refuses outright if `branch` is a protected branch (a PR whose head is
    main is nonsensical, and this is the one place that could be asked for
    one). Returns the real PR URL `gh` reports, or the error envelope -- never
    a fabricated URL.
    """
    if not branch or branch in PROTECTED_BRANCHES:
        return {"status": "error", "error": f"refusing to open a PR with protected head '{branch}'"}
    if not title or not title.strip():
        return {"status": "error", "error": "title must be non-empty"}

    argv = [
        "gh", "pr", "create",
        "--base", base,
        "--head", branch,
        "--title", title,
        "--body", body,
    ]
    try:
        result = subprocess.run(
            argv, cwd=str(REPO_ROOT), capture_output=True, text=True,
            timeout=GH_TIMEOUT_SECONDS, shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}

    if result.returncode != 0:
        return {"status": "error", "error": result.stderr.strip() or "gh pr create failed"}

    url = next((line.strip() for line in result.stdout.splitlines() if line.strip().startswith("http")), None)
    if not url:
        return {"status": "error", "error": f"gh pr create reported success but no URL: {result.stdout!r}"}

    return {"status": "ok", "url": url, "branch": branch, "base": base}
