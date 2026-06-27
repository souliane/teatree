r"""Full-tree banned-brand backstop scan (#1570).

The diff/payload banned-terms gate (``check-banned-terms.sh`` →
``banned_terms_scanner``) only ever sees a *change*: a staged diff, a
commit message, or a publish-surface body. A customer/tenant brand name
that is ALREADY committed is invisible to it forever — it never appears
in a post-landing diff. This module is the backstop the diff-only gate
cannot provide: it enumerates every git-tracked file and scans the
COMMITTED blob's content for the high-confidence brand list, so a
pre-existing committed brand name is caught on push-to-main and on a
schedule.

Two design choices distinguish it from the fast diff gate.

One shared matcher: the brand pass routes through
:func:`teatree.hooks.term_match.matched_term` — the SAME whole-token
matcher the ``[teatree].banned_terms`` posting gate and the
``[overlay_leak].terms`` core-leak gate use. ``-``, ``_``, whitespace,
punctuation AND camelCase boundaries all separate tokens, so a brand
glued into ``wt_777_<brand>`` or a camelCase ``AcmeConfig`` is caught
where a plain ``\b(term)\b`` regex would miss it. Routing through the one
matcher means the four banned-terms entry points cannot drift (pinned by
``tests/teatree_hooks/test_banned_terms_parity.py``).

Committed-blob read: a brand name may be committed but later edited out
of the working tree (or staged) — a working-tree-only edit must not hide
a committed leak from the backstop. The scan reads the ``HEAD`` blob via
``git show HEAD:<path>`` and falls back to the working-tree file only
when the blob is unavailable (a freshly-added, not-yet-committed file).

Email carve-out preserved: a brand that appears only inside an email
address (author/contact metadata) is allowed — :func:`term_match.strip_emails`
blanks emails before matching, exactly as the shell scanner does.

The brand list is read from ``[teatree].banned_brands`` in
``~/.teatree.toml`` (a NEW optional high-confidence key, distinct from
the flat ``banned_terms`` the shell gate consumes). The public repo
ships with no brands configured — each operator extends it locally.
"""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from teatree.hooks import term_match
from teatree.utils.run import CommandFailedError, TimeoutExpired, run_allowed_to_fail, run_checked

# Comma-separated brand list, used by CI where ``~/.teatree.toml`` is
# absent. Mirrors ``$TEATREE_OVERLAY_LEAK_TERMS`` for the overlay-leak
# gate so the public repo can enforce the backstop from a CI secret
# without committing any brand name. Takes precedence over the config.
_BRANDS_ENV = "TEATREE_BANNED_BRANDS"
_BRANDS_KEY = "banned_brands"

_GIT_LS_TIMEOUT_S = 30
_GIT_SHOW_TIMEOUT_S = 30

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


class BannedTermsUnsetError(RuntimeError):
    """The configured banned-terms/brands list is genuinely UNSET.

    Separates a genuinely-absent list — a missing config, an unloadable
    config, a missing key, or a wrong-typed value — from a DELIBERATE empty
    list (``key = []``). An unset list is refused LOUD so a load bug that
    silently returns nothing can never be mistaken for "the operator chose no
    terms"; an explicit empty list is allowed and returns an empty tuple. The
    message names the offending key and the deliberate-empty escape hatch so
    the fix is actionable.
    """

    @classmethod
    def for_key(cls, key: str, env_var: str | None = None) -> "BannedTermsUnsetError":
        env_hint = f" (or supply the ${env_var} secret)" if env_var else ""
        return cls(
            f"{key} is unset — set it explicitly (use `{key} = []` if you intend "
            f"no terms){env_hint}; refusing to run with an unloadable banned-terms list."
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
    """Load the high-confidence brand list, FAILING LOUD when it is unset.

    ``$TEATREE_BANNED_BRANDS`` (comma-separated) takes precedence so CI —
    where ``~/.teatree.toml`` does not exist — feeds the list from a secret;
    a set env var short-circuits before any raise. Otherwise reads
    ``[teatree].banned_brands`` from *config_path*. An explicit
    ``banned_brands = []`` is the operator's deliberate no-brands choice and
    returns an empty tuple. A genuinely-unset list — no config, an unloadable
    config, a missing key, or a wrong-typed value — raises
    :class:`BannedTermsUnsetError`: an unset list is too dangerous to
    scan as empty because a load bug would look identical to a deliberate
    no-brands choice.
    """
    env = os.environ.get(_BRANDS_ENV, "")
    if env.strip():
        return tuple(t.strip() for t in env.split(",") if t.strip())
    if not config_path.is_file():
        raise BannedTermsUnsetError.for_key(_BRANDS_KEY, _BRANDS_ENV)
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise BannedTermsUnsetError.for_key(_BRANDS_KEY, _BRANDS_ENV) from exc
    section = data.get("teatree")
    brands = section.get("banned_brands") if isinstance(section, dict) else None
    if not isinstance(brands, list):
        raise BannedTermsUnsetError.for_key(_BRANDS_KEY, _BRANDS_ENV)
    return tuple(str(t).strip() for t in brands if isinstance(t, str) and t.strip())


def scan_text(text: str, terms: tuple[str, ...]) -> list[tuple[int, str, str]]:
    """Scan *text* line by line for brand hits; return ``(lineno, term, line)``.

    Routes through the shared :func:`term_match.matched_term` with the
    email carve-out applied per line, so the brand pass matches the other
    banned-terms entry points exactly. Empty *terms* is a clean no-op.
    """
    if not terms:
        return []
    hits: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        term = term_match.matched_term(term_match.strip_emails(line), terms)
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


def committed_blob_text(repo_root: Path, rel_path: str) -> str | None:
    """Return the ``HEAD`` blob content of *rel_path*, or ``None`` if unavailable.

    Reading the COMMITTED blob (not the working tree) is what makes the
    backstop hold against a staged/working-tree edit that removes a brand
    name from the file but leaves it in the last commit: the working-tree
    file would look clean while the committed leak persists. ``None`` is
    returned when ``git show`` cannot resolve the blob — a freshly-added
    file with no commit yet, a detached/empty ``HEAD``, or git being
    unavailable — so the caller can fall back to the working-tree content.
    """
    try:
        result = run_allowed_to_fail(
            ["git", "-C", str(repo_root), "show", f"HEAD:{rel_path}"],
            expected_codes=None,
            timeout=_GIT_SHOW_TIMEOUT_S,
        )
    except (TimeoutExpired, OSError, UnicodeDecodeError):
        # A binary blob does not decode as text — treat it as unscannable
        # (caller skips it), exactly as the working-tree binary read does.
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _scannable_text(repo_root: Path, path: Path, rel: str) -> str | None:
    """The text to scan for *path*: the committed blob, else the working tree.

    Prefer the committed ``HEAD`` blob so a working-tree-only edit cannot
    hide a committed brand. Fall back to the working-tree file only when no
    committed blob exists (a newly-added, not-yet-committed tracked file).
    """
    blob = committed_blob_text(repo_root, rel)
    if blob is not None:
        return blob
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def scan_tree(repo_root: Path, terms: tuple[str, ...]) -> list[TreeFinding]:
    """Scan every tracked text file for committed brands and conflated terminology.

    Two passes per file, both over the COMMITTED blob (so a working-tree
    edit cannot hide a committed leak): the operator-supplied
    high-confidence brand list (a clean no-op when none is configured) and
    the built-in terminology gate (``terminology_gate``), which flags
    teatree-internal vocabulary conflations regardless of any operator
    config.
    """
    from teatree.hooks import terminology_gate  # noqa: PLC0415

    findings: list[TreeFinding] = []
    for path in git_tracked_files(repo_root):
        rel = path.relative_to(repo_root).as_posix()
        text = _scannable_text(repo_root, path, rel)
        if text is None:
            continue
        lines = text.splitlines()
        findings.extend(TreeFinding(rel, lineno, term, line) for lineno, term, line in scan_text(text, terms))
        if not terminology_gate.path_is_exempt(rel):
            for lineno, finding in terminology_gate.scan_text(text):
                term = f"{finding.phrase} — {finding.correction}"
                findings.append(TreeFinding(rel, lineno, term, lines[lineno - 1]))
    return findings
