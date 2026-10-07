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

import difflib
import re
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


# ---- where the code lives, and what a wrong path probably meant --------------------------
#
# The investigator used to be handed a deployment marker and nothing else, and a marker is
# a label: it appears in chaos tooling and deployment records, never in the service's own
# source. With no directory listing and no hint it guessed paths that did not exist. These
# give it the service's directory and the layout around it, and a wrong path gets the
# nearest real one back.

MAX_LAYOUT_FILES = 40
SERVICES_PARENT = "services"
_SERVICE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_MIN_SUBSTRING_LENGTH = 4
_SUGGESTION_CUTOFF = 0.75


def service_source_dir(service_name: str) -> str | None:
    """`services/<name>` if that directory exists, else None.

    The name must be a single path segment, so it can only ever name a direct child of
    `services/` and never walk out of it. Whether the directory exists is checked, not
    assumed: a repo that keeps its services elsewhere gets None and the layout instead.
    """
    if not isinstance(service_name, str) or not _SERVICE_NAME.fullmatch(service_name):
        return None
    if (REPO_ROOT / SERVICES_PARENT / service_name).is_dir():
        return f"{SERVICES_PARENT}/{service_name}"
    return None


def _relative(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT)).replace("\\", "/")


def _visible_children(parent: Path) -> list[Path]:
    try:
        entries = sorted(parent.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return []
    return [e for e in entries if not e.name.startswith(".") and e.name not in EXCLUDED_DIR_NAMES]


def repo_context(service_name: str) -> dict:
    """The repository's top-level layout and the service's source files, for the prompt.

    Never raises: a layout that cannot be read is an emptier context, not a stopped
    investigation. File lists are capped, and say when they were.
    """
    top_level = [e.name + "/" if e.is_dir() else e.name for e in _visible_children(REPO_ROOT)][:MAX_LAYOUT_FILES]
    service_dir = service_source_dir(service_name)
    files: list[str] = []
    if service_dir:
        try:
            files = sorted(_relative(p) for p in _iter_source_files(REPO_ROOT / service_dir))
        except OSError:
            files = []
    return {
        "top_level": top_level,
        "service_dir": service_dir,
        "service_files": files[:MAX_LAYOUT_FILES],
        "service_files_truncated": len(files) > MAX_LAYOUT_FILES,
    }


def _normalise(name: str) -> str:
    return name.lower().replace("_", "-")


def _closest_child(parent: Path, name: str) -> Path | None:
    """The child of `parent` that `name` most plausibly meant, or None."""
    children = _visible_children(parent)
    wanted = _normalise(name)
    by_name = {_normalise(c.name): c for c in children}
    if wanted in by_name:
        return by_name[wanted]
    if len(wanted) >= _MIN_SUBSTRING_LENGTH:
        for norm, child in by_name.items():
            if wanted in norm:
                return child
    close = difflib.get_close_matches(wanted, list(by_name), n=1, cutoff=_SUGGESTION_CUTOFF)
    return by_name[close[0]] if close else None


def suggest_paths(path: str, limit: int = 3) -> list[str]:
    """Existing paths a wrong `path` probably meant. Always real paths, never a guess.

    A segment that does not exist is matched against its real siblings (so
    `services/payment_service/app.py` finds `services/payment-service/app.py`); a name that
    matches nothing there is looked for one level down in every directory (so a bare
    `payment_service` or `worker` finds `services/payment-service` and `services/worker-service`).
    """
    parts = [p for p in Path(str(path).replace("\\", "/")).parts if p not in ("", ".", "/")]
    if not parts or ".." in parts:
        return []
    try:
        current = REPO_ROOT
        for index, part in enumerate(parts):
            if (current / part).exists():
                current = current / part
                continue
            rest = parts[index + 1:]
            sibling = _closest_child(current, part)
            if sibling is not None:
                corrected = sibling.joinpath(*rest) if rest else sibling
                return [_relative(corrected if corrected.exists() else sibling)]
            wanted = _normalise(part)
            found = {}
            for top in _visible_children(REPO_ROOT):
                if not top.is_dir():
                    continue
                for child in _visible_children(top):
                    norm = _normalise(child.name)
                    if norm == wanted or (len(wanted) >= _MIN_SUBSTRING_LENGTH and wanted in norm):
                        corrected = child.joinpath(*rest) if rest else child
                        target = corrected if corrected.exists() else child
                        # A wrong path is nearly always a directory the caller meant to search or
                        # read inside, so directories rank ahead of files, an exact name ahead of
                        # a longer one that merely contains it, then the shorter name.
                        found[_relative(target)] = (not target.is_dir(), norm != wanted, len(norm))
            return sorted(found, key=lambda p: (*found[p], p))[:limit]
    except OSError:
        return []
    return []


def _did_you_mean(path: str) -> str:
    suggestions = suggest_paths(path)
    return f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""


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
        return {"status": "error", "error": f"path '{path or '.'}' does not exist.{_did_you_mean(path) if path else ''}"}

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
        return {"status": "error", "error": f"'{path}' does not exist in the repository.{_did_you_mean(path)}"}
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
