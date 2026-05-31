r"""Full-tree banned-brand backstop scan (#1570).

The diff/payload banned-terms gate (``check-banned-terms.sh`` →
``banned_terms_scanner``) only ever sees a *change*: a staged diff, a
commit message, or a publish-surface body. A customer/tenant brand name
that is ALREADY committed is invisible to it forever — it never appears
in a post-landing diff. This module is the backstop the diff-only gate
cannot provide: it enumerates every git-tracked file and scans its full
CONTENT for the high-confidence brand list, so a pre-existing committed
brand name is caught on push-to-main and on a schedule.

Two design choices distinguish it from the fast diff gate.

High-confidence list only: the matcher is underscore-tolerant. ``\b``
treats ``_`` as a word char, so a brand glued into ``wt_777_<brand>`` is
never bounded by ``\b`` and slips through. The boundary is replaced with
one that treats ``_`` (and the other word joiners) as a separator. That
loosening is safe ONLY for high-confidence brand tokens; applying it to
common-word entries would surface substring noise, so the common-word
``banned_terms`` list keeps its ``\b`` matching in the unchanged shell
scanner.

Email carve-out preserved: a brand that appears only inside an email
address (author/contact metadata) is allowed, exactly as the shell
scanner does, so legitimate addresses are not flagged.

The brand list is read from ``[teatree].banned_brands`` in
``~/.teatree.toml`` (a NEW optional high-confidence key, distinct from
the flat ``banned_terms`` the shell gate consumes). The public repo
ships with no brands configured — each operator extends it locally.
"""

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from teatree.utils.run import CommandFailedError, TimeoutExpired, run_checked

# Comma-separated brand list, used by CI where ``~/.teatree.toml`` is
# absent. Mirrors ``$TEATREE_OVERLAY_LEAK_TERMS`` for the overlay-leak
# gate so the public repo can enforce the backstop from a CI secret
# without committing any brand name. Takes precedence over the config.
_BRANDS_ENV = "TEATREE_BANNED_BRANDS"

# Word joiners that ``\b`` wrongly treats as part of the word, hiding a
# brand glued onto an identifier (``wt_777_<brand>``, ``<brand>_x``). The
# tree matcher treats each as a separator so the brand is caught on
# either side. ``\b`` already handles whitespace/punctuation boundaries.
_JOINERS = "_"

# Mirrors check-banned-terms.sh: a brand that appears only inside an email
# address is author/contact metadata and is allowed.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

_GIT_LS_TIMEOUT_S = 30

# Suffixes that hold scannable text. A tracked binary (image, archive)
# is skipped — it cannot carry a readable brand name and may not decode.
_TEXT_SUFFIXES: frozenset[str] = frozenset(
    {
        ".py",
        ".pyi",
        ".md",
        ".rst",
        ".txt",
        ".html",
        ".htm",
        ".css",
        ".yml",
        ".yaml",
        ".toml",
        ".json",
        ".cfg",
        ".ini",
        ".sh",
        ".bash",
        ".zsh",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".sql",
        ".env",
        ".j2",
        ".jinja",
        ".jinja2",
        ".tmpl",
        ".dockerfile",
    }
)


@dataclass(frozen=True)
class TreeFinding:
    """A single banned-brand hit in a committed file."""

    path: str
    lineno: int
    term: str
    line: str

    def render(self) -> str:
        return f"{self.path}:{self.lineno}: {self.term!r} — {self.line.strip()}"


def load_brand_terms(config_path: Path) -> tuple[str, ...]:
    """Load the high-confidence brand list.

    ``$TEATREE_BANNED_BRANDS`` (comma-separated) takes precedence so CI —
    where ``~/.teatree.toml`` does not exist — feeds the list from a
    secret. Otherwise reads ``[teatree].banned_brands`` from *config_path*.
    Returns an empty tuple when neither source declares any brand — the
    scan is then a clean no-op, matching the public repo's tenant-agnostic
    default.
    """
    env = os.environ.get(_BRANDS_ENV, "")
    if env.strip():
        return tuple(t.strip() for t in env.split(",") if t.strip())
    if not config_path.is_file():
        return ()
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return ()
    section = data.get("teatree", {})
    brands = section.get("banned_brands", []) if isinstance(section, dict) else []
    if not isinstance(brands, list):
        return ()
    return tuple(str(t).strip() for t in brands if isinstance(t, str) and t.strip())


def build_brand_pattern(terms: tuple[str, ...]) -> re.Pattern[str] | None:
    r"""Compile an underscore-tolerant pattern over the high-confidence brands.

    Each side of the term is bounded by EITHER a regular ``\b`` boundary
    OR a word-joiner (``_``) — so ``wt_777_<brand>`` and ``<brand>_x`` are
    caught where a plain ``\b(term)\b`` would miss them. Longer terms sort
    first so an alternation prefers the most specific match.
    """
    cleaned = [t for t in terms if t]
    if not cleaned:
        return None
    cleaned.sort(key=len, reverse=True)
    escaped = "|".join(re.escape(t) for t in cleaned)
    joiners = re.escape(_JOINERS)
    # A boundary that is satisfied by a word boundary OR an adjacent joiner.
    left = rf"(?:\b|(?<=[{joiners}]))"
    right = rf"(?:\b|(?=[{joiners}]))"
    return re.compile(rf"{left}(?:{escaped}){right}", re.IGNORECASE)


def _line_has_non_email_match(line: str, pattern: re.Pattern[str]) -> str | None:
    """Return the first brand hit on *line* that is not inside an email, else None."""
    email_spans = [m.span() for m in _EMAIL_RE.finditer(line)]
    for match in pattern.finditer(line):
        if any(start <= match.start() and match.end() <= end for start, end in email_spans):
            continue
        return match.group(0)
    return None


def scan_text(text: str, pattern: re.Pattern[str]) -> list[tuple[int, str, str]]:
    """Scan *text* line by line; return ``(lineno, matched_term, line)`` hits."""
    hits: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        term = _line_has_non_email_match(line, pattern)
        if term is not None:
            hits.append((lineno, term, line))
    return hits


def git_tracked_files(repo_root: Path) -> list[Path]:
    """Enumerate git-tracked text files under *repo_root*.

    Uses ``git ls-files`` (the same source the shell gate's pre-commit
    invocation feeds from) and keeps only the text suffixes the scanner
    can read. Returns an empty list if git is unavailable or the path is
    not a repo — the caller treats that as a clean (no-files) scan.
    """
    try:
        result = run_checked(
            ["git", "-C", str(repo_root), "ls-files", "-z"],
            timeout=_GIT_LS_TIMEOUT_S,
        )
    except (CommandFailedError, TimeoutExpired, OSError):
        return []
    names = [n for n in result.stdout.split("\0") if n]
    return [repo_root / n for n in names if (repo_root / n).suffix.lower() in _TEXT_SUFFIXES]


def scan_tree(repo_root: Path, terms: tuple[str, ...]) -> list[TreeFinding]:
    """Scan every tracked text file's content for high-confidence brands."""
    pattern = build_brand_pattern(terms)
    if pattern is None:
        return []
    findings: list[TreeFinding] = []
    for path in git_tracked_files(repo_root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = path.relative_to(repo_root).as_posix()
        findings.extend(TreeFinding(rel, lineno, term, line) for lineno, term, line in scan_text(text, pattern))
    return findings
