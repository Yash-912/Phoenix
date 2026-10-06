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

import hashlib
import json
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


# Only Phoenix's own PRs are compared. Their diffs are the only ones Phoenix can
# be duplicating, and it keeps the lookup to a handful of `gh pr diff` calls.
PHOENIX_TITLE_PREFIX = "[Phoenix Tier 3]"
LIST_LIMIT = 100


def diff_fingerprint(diff: str) -> str | None:
    """What a diff changes, independent of where and on which base it was cut.

    The changed file paths plus the removed and added lines, with trailing whitespace
    and line endings dropped. Hunk offsets and blob hashes are left out: the same fix
    cut from a slightly different commit shifts both and is still the same change.
    None when there is no changed line to fingerprint, so an empty or unreadable diff
    can never match anything.
    """
    files: list[str] = []
    changed: list[str] = []
    for raw in (diff or "").splitlines():
        line = raw.rstrip()
        if line.startswith("diff --git "):
            files.append(line.split(" b/", 1)[-1])
        elif line.startswith(("+++", "---")):
            continue
        elif line[:1] in ("+", "-"):
            changed.append(line)
    if not changed:
        return None
    payload = "\n".join(sorted(set(files)) + ["--"] + changed)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _gh(argv: list[str]) -> subprocess.CompletedProcess | dict:
    try:
        return subprocess.run(
            argv, cwd=str(REPO_ROOT), capture_output=True, text=True,
            timeout=GH_TIMEOUT_SECONDS, shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}


def find_open_duplicate(diff: str) -> dict:
    """The open Phoenix PR that already carries this change, if there is one.

    Read-only. `{"status": "ok", "duplicate": {number, url, branch} | None}`, or the
    error envelope when the answer cannot be given: a failed or unreadable listing, or
    a diff with nothing to compare. An error is not "no duplicate", and the caller says
    so. A single PR whose diff cannot be read is skipped rather than failing the lookup.
    """
    fingerprint = diff_fingerprint(diff)
    if fingerprint is None:
        return {"status": "error", "error": "the diff has no changed lines to compare"}

    listed = _gh(["gh", "pr", "list", "--state", "open", "--limit", str(LIST_LIMIT),
                  "--json", "number,url,title,headRefName"])
    if isinstance(listed, dict):
        return listed
    if listed.returncode != 0:
        return {"status": "error", "error": listed.stderr.strip() or "gh pr list failed"}
    try:
        prs = json.loads(listed.stdout)
        candidates = [pr for pr in prs if str(pr.get("title", "")).startswith(PHOENIX_TITLE_PREFIX)]
    except (ValueError, AttributeError, TypeError):
        return {"status": "error", "error": "gh pr list returned something that is not a PR listing"}

    for pr in candidates:
        number = pr.get("number")
        if not isinstance(number, int):
            continue
        fetched = _gh(["gh", "pr", "diff", str(number)])
        if isinstance(fetched, dict) or fetched.returncode != 0:
            continue
        if diff_fingerprint(fetched.stdout) == fingerprint:
            return {"status": "ok", "duplicate": {"number": number, "url": pr.get("url"), "branch": pr.get("headRefName")}}
    return {"status": "ok", "duplicate": None}


def comment_on_pull_request(number: int, body: str) -> dict:
    """`gh pr comment <number> --body ...` -- a note on a PR that already exists.

    Posting a comment is the whole capability: nothing here can approve, merge or
    close. `number` must be an int, so it can never be read as a flag or a path.
    """
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        return {"status": "error", "error": f"refusing to comment on '{number}': not a PR number"}
    if not body or not body.strip():
        return {"status": "error", "error": "comment body must be non-empty"}

    result = _gh(["gh", "pr", "comment", str(number), "--body", body])
    if isinstance(result, dict):
        return result
    if result.returncode != 0:
        return {"status": "error", "error": result.stderr.strip() or "gh pr comment failed"}
    return {"status": "ok", "number": number}
