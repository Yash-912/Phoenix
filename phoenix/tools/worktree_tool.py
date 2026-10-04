"""Applying a Tier 3 patch only ever happens inside a disposable git worktree,
never on the checkout this process itself is running from.

Mirrors chaos/lib/deployer.py's discipline: every subprocess call is a fixed
argv run with shell=False, every caller-supplied value (branch name, file
path) is validated against an allowlisted shape first, and nothing here
trusts a string enough to hand it to a shell. The one new risk this module
introduces -- writing LLM-authored content to disk -- is bounded by requiring
the write to land inside the worktree this module itself just created, which
is checked the same way phoenix/tools/repo_tool.py checks a read.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKTREE_PARENT = REPO_ROOT / ".phoenix_worktrees"
GIT_TIMEOUT_SECONDS = 60

_SAFE_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")

# Never a destination this module will create a worktree/branch for, and
# never a branch pr_opener is allowed to push to or open a PR against as its
# head. The protected-branch boundary is enforced here, independent of
# anything remediation_policy decides, so it holds even if a future caller
# gets the category routing wrong.
PROTECTED_BRANCHES = {"main", "master"}


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
        shell=False,
    )


def _safe_branch(branch: str) -> bool:
    return bool(branch) and bool(_SAFE_BRANCH.match(branch)) and branch not in PROTECTED_BRANCHES


def current_branch() -> str | None:
    """The branch the primary checkout is on, or None when HEAD is detached.

    A Tier 3 PR must target the branch its patch was based on. The worktree
    is created from this checkout's HEAD, so a PR against any other branch
    would carry every commit that differs between the two as if the agent had
    written it.
    """
    result = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=REPO_ROOT)
    name = result.stdout.strip()
    return name if result.returncode == 0 and name and name != "HEAD" else None


def new_branch_name(incident_id: int, service_name: str) -> str:
    """A unique, filesystem- and git-safe branch name for one Tier 3 attempt."""
    slug = re.sub(r"[^a-z0-9]+", "-", service_name.lower()).strip("-")
    return f"phoenix/tier3-incident-{incident_id}-{slug}-{int(time.time())}"


def create_worktree(branch: str) -> dict:
    """`git worktree add -b <branch> <path> HEAD` -- a fresh checkout at the
    current HEAD, isolated from the primary working tree.

    Refuses a branch name that is not _SAFE_BRANCH, or that names a protected
    branch, before any subprocess runs.
    """
    if not _safe_branch(branch):
        return {"status": "error", "error": f"'{branch}' is not a safe/allowed branch name"}

    WORKTREE_PARENT.mkdir(parents=True, exist_ok=True)
    path = WORKTREE_PARENT / branch.replace("/", "__")

    try:
        result = _run_git(["worktree", "add", "-b", branch, str(path), "HEAD"], cwd=REPO_ROOT)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}

    if result.returncode != 0:
        return {"status": "error", "error": result.stderr.strip() or "git worktree add failed"}

    return {"status": "ok", "path": str(path), "branch": branch}


def write_file_in_worktree(worktree_path: str, file_path: str, content: str) -> dict:
    """Write `content` to `file_path` inside the worktree -- and only inside it.

    `file_path` is resolved against the worktree root and checked the same
    way repo_tool checks a read against REPO_ROOT: resolve first, then verify
    the result is still under the worktree, so a path that traverses out
    (into the primary checkout, or anywhere else) is refused rather than
    silently followed.
    """
    worktree = Path(worktree_path).resolve()
    if not worktree.is_dir():
        return {"status": "error", "error": f"worktree '{worktree_path}' does not exist"}

    try:
        target = (worktree / file_path).resolve()
        target.relative_to(worktree)
    except (OSError, ValueError):
        return {"status": "error", "error": f"'{file_path}' escapes the worktree"}
    if not target.exists():
        return {"status": "error", "error": f"'{file_path}' does not exist in the worktree"}

    try:
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        return {"status": "error", "error": str(exc)}

    return {"status": "ok", "path": str(target.relative_to(worktree)).replace("\\", "/")}


def commit_patch(worktree_path: str, file_path: str, message: str) -> dict:
    """Stage exactly `file_path` and commit it inside the worktree.

    Staging the one named path rather than `git add -A` is itself a scope
    guard: nothing else the worktree happens to contain (an untracked file,
    a stray artifact) can ride along into the commit this becomes the diff
    for.
    """
    worktree = Path(worktree_path).resolve()
    if not worktree.is_dir():
        return {"status": "error", "error": f"worktree '{worktree_path}' does not exist"}

    add = _run_git(["add", "--", file_path], cwd=worktree)
    if add.returncode != 0:
        return {"status": "error", "error": add.stderr.strip() or "git add failed"}

    commit = _run_git(
        [
            "-c", "user.name=phoenix-tier3",
            "-c", "user.email=phoenix-tier3@localhost",
            "commit", "-m", message,
        ],
        cwd=worktree,
    )
    if commit.returncode != 0:
        return {"status": "error", "error": commit.stderr.strip() or "git commit failed"}

    sha = _run_git(["rev-parse", "HEAD"], cwd=worktree)
    if sha.returncode != 0:
        return {"status": "error", "error": "commit succeeded but HEAD could not be resolved"}

    return {"status": "ok", "commit_sha": sha.stdout.strip()}


def diff_against_base(worktree_path: str, base: str = "HEAD~1") -> dict:
    """What the worktree's one commit actually changed, read back from git
    rather than trusted from the patch candidate -- this is the "inspect the
    final diff" step the validator re-checks scope against.
    """
    worktree = Path(worktree_path).resolve()
    result = _run_git(["diff", "--no-color", f"{base}..HEAD"], cwd=worktree)
    if result.returncode != 0:
        return {"status": "error", "error": result.stderr.strip() or "git diff failed"}
    files = _run_git(["diff", "--name-only", f"{base}..HEAD"], cwd=worktree)
    changed_files = [f for f in files.stdout.splitlines() if f.strip()] if files.returncode == 0 else []
    return {"status": "ok", "diff": result.stdout, "changed_files": changed_files}


def push_branch(worktree_path: str, branch: str) -> dict:
    """Push the worktree's branch to origin -- never to a protected branch,
    enforced here independently of whatever called this."""
    if not _safe_branch(branch):
        return {"status": "error", "error": f"'{branch}' is not a safe/allowed branch name"}

    worktree = Path(worktree_path).resolve()
    result = _run_git(["push", "-u", "origin", f"HEAD:refs/heads/{branch}"], cwd=worktree)
    if result.returncode != 0:
        return {"status": "error", "error": result.stderr.strip() or "git push failed"}
    return {"status": "ok", "branch": branch}


def discard_worktree(worktree_path: str, branch: str | None = None) -> dict:
    """Remove the worktree and (best-effort) its local branch.

    Called on any Tier 3 failure path so a rejected or failed patch never
    lingers as stray state next to the primary checkout. Best-effort: a
    worktree that is already gone, or a branch git refuses to delete, is
    reported rather than raised -- cleanup failing must not mask the
    validation failure that triggered it.
    """
    worktree = Path(worktree_path).resolve()
    errors = []
    if worktree.exists():
        result = _run_git(["worktree", "remove", "--force", str(worktree)], cwd=REPO_ROOT)
        if result.returncode != 0:
            errors.append(result.stderr.strip())
    if branch and _safe_branch(branch):
        _run_git(["branch", "-D", branch], cwd=REPO_ROOT)  # best-effort, local only

    return {"status": "ok" if not errors else "error", "errors": errors}
