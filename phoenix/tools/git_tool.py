"""Read-only git history for the Tier 3 code investigator.

Both functions shell out to the real `git` binary against this checkout --
nothing here fabricates a commit or a diff. The same discipline
chaos/lib/deployer.py uses for compose applies here: a fixed argv, shell=False,
and every caller-supplied value validated against an allowlisted shape before
it reaches the subprocess, so there is no string path from a hypothesis or an
LLM's free text to an arbitrary git/shell flag.

Refs are validated with _SAFE_REF rather than passed through: git treats an
argument starting with "-" as a flag (ref="--upload-pack=touch x" would try to
run an arbitrary command if it ever reached `git log`), so any ref that could
be mistaken for an option is refused before subprocess.run ever sees it.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GIT_TIMEOUT_SECONDS = 20

# Deliberately conservative: short SHAs, branch/tag names, and the relative
# forms (~, ^) git actually uses. Nothing here can start with "-", so a ref
# cannot be mistaken for a flag by git itself.
_SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/~^-]{0,99}$")

MAX_COMMITS = 50
DEFAULT_COMMITS = 10
MAX_DIFF_CHARS = 20_000


def _safe_ref(ref: str) -> bool:
    return bool(ref) and bool(_SAFE_REF.match(ref))


def _safe_path(path: str | None) -> Path | None:
    """`path` resolved under REPO_ROOT, or None if absent/escaping.

    None is a legal return for "no path filter was given" (path=None) as well
    as for "the given path escapes the repo" -- callers distinguish the two by
    checking the original argument, which both get_git_commits and
    get_git_diff already do before calling this.
    """
    if not path:
        return None
    if Path(path).is_absolute():
        return None
    try:
        resolved = (REPO_ROOT / path).resolve()
        resolved.relative_to(REPO_ROOT)
    except (OSError, ValueError):
        return None
    return resolved


def _run_git(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
        shell=False,
    )


def get_git_commits(path: str | None = None, limit: int = DEFAULT_COMMITS) -> dict:
    """Recent real commit history, optionally scoped to one file/directory.

    Returns {"status": "ok", "commits": [{"sha", "author", "date", "subject"}]}
    newest-first (git log's own order), or the error envelope.
    """
    limit = max(1, min(int(limit or DEFAULT_COMMITS), MAX_COMMITS))

    scoped: Path | None = None
    if path is not None:
        scoped = _safe_path(path)
        if scoped is None:
            return {"status": "error", "error": f"path '{path}' is outside the repository"}

    args = ["log", f"-n{limit}", "--date=iso-strict", "--pretty=format:%H%x1f%an%x1f%ad%x1f%s%x1e"]
    if scoped is not None:
        args += ["--", str(scoped)]

    try:
        result = _run_git(args)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}

    if result.returncode != 0:
        return {"status": "error", "error": result.stderr.strip() or "git log failed"}

    commits = []
    for record in result.stdout.split("\x1e"):
        record = record.strip()
        if not record:
            continue
        parts = record.split("\x1f")
        if len(parts) != 4:
            continue
        sha, author, date, subject = parts
        commits.append({"sha": sha, "author": author, "date": date, "subject": subject})

    return {"status": "ok", "commits": commits}


def get_git_diff(base: str = "HEAD~1", head: str = "HEAD", path: str | None = None) -> dict:
    """The real diff between two refs, optionally scoped to one file/directory.

    Returns {"status": "ok", "diff": "<unified diff text>"} or the error
    envelope. The diff is truncated at MAX_DIFF_CHARS for the audit trail and
    the LLM's context; `truncated` says so rather than silently cutting it.
    """
    if not _safe_ref(base):
        return {"status": "error", "error": f"'{base}' is not a valid git ref"}
    if not _safe_ref(head):
        return {"status": "error", "error": f"'{head}' is not a valid git ref"}

    scoped: Path | None = None
    if path is not None:
        scoped = _safe_path(path)
        if scoped is None:
            return {"status": "error", "error": f"path '{path}' is outside the repository"}

    args = ["diff", "--no-color", f"{base}..{head}"]
    if scoped is not None:
        args += ["--", str(scoped)]

    try:
        result = _run_git(args)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}

    if result.returncode != 0:
        return {"status": "error", "error": result.stderr.strip() or "git diff failed"}

    diff = result.stdout
    truncated = len(diff) > MAX_DIFF_CHARS
    return {
        "status": "ok",
        "base": base,
        "head": head,
        "diff": diff[:MAX_DIFF_CHARS],
        "truncated": truncated,
    }
