"""Read-only repository access for the Tier 3 code investigator.

Two tools, both scoped to the repository root and nothing outside it:
search_repository (find candidate files/lines) and read_file (inspect one).
Neither can escape REPO_ROOT -- every path is resolved and checked against it
before anything is opened -- and neither can run a shell command. This is the
whole of Tier 3's filesystem reach; there is no third tool that writes.

Why resolve-then-check rather than a prefix string comparison: a prefix check
on the unresolved string passes "../../etc/passwd".startswith("D:/Agentic")
trivially (it doesn't, but a cleverer traversal can be built to), while
Path.resolve() collapses ".." segments before the comparison ever runs, so
the check is against where the path actually lands, not how it was spelled.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Directories no investigation should ever read into: version control
# internals, virtualenvs/dependencies, and caches. Excluding them is what
# keeps search_repository's results about the application, not about every
# third-party package a venv happens to vendor.
EXCLUDED_DIR_NAMES = {
    ".git", "agentic", "node_modules", "__pycache__", ".pytest_cache",
    ".phoenix_worktrees", "venv", ".venv", "dist", "build", ".mypy_cache",
}

MAX_READ_LINES = 400
MAX_SEARCH_RESULTS = 30
MAX_MATCHES_PER_FILE = 5
# Binary/media extensions search_repository never opens -- a repo of any
# size has images and compiled artifacts, and decoding one as text either
# raises or returns noise no investigation can use.
SKIPPED_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".pyc", ".so",
    ".dll", ".exe", ".db", ".sqlite", ".woff", ".woff2", ".ttf",
}


def _resolve_in_repo(rel_path: str) -> Path | None:
    """`rel_path` resolved under REPO_ROOT, or None if it escapes or is absolute.

    An absolute path is refused outright rather than joined: Path(a) / b
    discards `a` entirely when `b` is itself absolute (POSIX) or drive-rooted
    (Windows), so joining would silently let an absolute `rel_path` resolve to
    itself regardless of REPO_ROOT, defeating the check that follows it.
    """
    if not rel_path or Path(rel_path).is_absolute():
        return None
    try:
        candidate = (REPO_ROOT / rel_path).resolve()
    except (OSError, ValueError):
        return None
    try:
        candidate.relative_to(REPO_ROOT)
    except ValueError:
        return None
    return candidate


def _iter_source_files(start: Path):
    for path in start.rglob("*"):
        if not path.is_file():
            continue
        if any(part in EXCLUDED_DIR_NAMES for part in path.relative_to(REPO_ROOT).parts):
            continue
        if path.suffix.lower() in SKIPPED_EXTENSIONS:
            continue
        yield path


def search_repository(query: str, path: str | None = None, max_results: int = MAX_SEARCH_RESULTS) -> dict:
    """Case-insensitive literal search for `query` across text files under
    `path` (default: the whole repo), scoped to REPO_ROOT.

    Returns {"status": "ok", "matches": [{"file", "line", "text"}, ...]} or
    the {"status": "error", "error": ...} envelope scoring._is_usable reads.
    Never raises: a file that cannot be decoded as UTF-8 is skipped, not
    fatal to the search.
    """
    if not query or not query.strip():
        return {"status": "error", "error": "query must be a non-empty string"}

    start = REPO_ROOT
    if path:
        resolved = _resolve_in_repo(path)
        if resolved is None:
            return {"status": "error", "error": f"path '{path}' is outside the repository"}
        start = resolved

    if not start.exists():
        return {"status": "error", "error": f"path '{path or '.'}' does not exist"}

    needle = query.lower()
    matches: list[dict] = []
    files = [start] if start.is_file() else _iter_source_files(start)

    for file_path in files:
        if len(matches) >= max_results:
            break
        try:
            text = file_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        per_file = 0
        for lineno, line in enumerate(text.splitlines(), start=1):
            if needle in line.lower():
                matches.append({
                    "file": str(file_path.relative_to(REPO_ROOT)).replace("\\", "/"),
                    "line": lineno,
                    "text": line.strip()[:200],
                })
                per_file += 1
                if per_file >= MAX_MATCHES_PER_FILE or len(matches) >= max_results:
                    break

    return {"status": "ok", "query": query, "matches": matches, "truncated": len(matches) >= max_results}


def read_file(path: str, start_line: int | None = None, end_line: int | None = None) -> dict:
    """The text of `path` (optionally one line range), scoped to REPO_ROOT.

    Capped at MAX_READ_LINES per call so one read cannot pull an entire large
    file into the LLM's context; a caller that needs more pages through
    start_line/end_line across more than one call.
    """
    resolved = _resolve_in_repo(path)
    if resolved is None:
        return {"status": "error", "error": f"path '{path}' is outside the repository"}
    if not resolved.exists():
        return {"status": "error", "error": f"'{path}' does not exist in the repository"}
    if not resolved.is_file():
        return {"status": "error", "error": f"'{path}' is not a file"}
    if resolved.suffix.lower() in SKIPPED_EXTENSIONS:
        return {"status": "error", "error": f"'{path}' is a binary/media file, not readable as text"}

    try:
        text = resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return {"status": "error", "error": f"'{path}' is not valid UTF-8 text"}
    except OSError as exc:
        return {"status": "error", "error": str(exc)}

    lines = text.splitlines()
    total = len(lines)
    lo = max(1, start_line or 1)
    hi = min(total, end_line or total, lo + MAX_READ_LINES - 1)
    if lo > total:
        return {"status": "error", "error": f"start_line {lo} is past end of file ({total} lines)"}

    snippet = "\n".join(lines[lo - 1:hi])
    return {
        "status": "ok",
        "path": str(resolved.relative_to(REPO_ROOT)).replace("\\", "/"),
        "start_line": lo,
        "end_line": hi,
        "total_lines": total,
        "truncated": hi < (end_line or total),
        "content": snippet,
    }
