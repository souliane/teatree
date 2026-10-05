r"""Scan tracked tree content for registry leak-class terms and terminology.

The classed ``banned_term_registry`` is the sole operator term source. The tree
pass refuses an absent registry or empty leak list.
"""

from dataclasses import dataclass
from pathlib import Path

from teatree.hooks import term_match
from teatree.utils.run import TimeoutExpired, run_allowed_to_fail

_GIT_LS_TIMEOUT_S = 30
_GIT_SHOW_TIMEOUT_S = 30

# Suffixes whose content is not decodable text. Everything ELSE is scanned: the
# gate reads the committed blob and skips whatever fails to decode, so a suffix
# it does not recognise costs one failed read, while EXCLUDING it costs a
# permanently-invisible committed leak. The prior allowlist of "text suffixes"
# silently dropped every tracked text file without one — ``Dockerfile``,
# ``Makefile``, ``NOTICE``, ``CODEOWNERS``, ``.gitignore``, an extension-less
# script — from a backstop whose whole job is to see what the diff gate cannot.
_BINARY_SUFFIXES: frozenset[str] = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".ico",
        ".webp",
        ".tif",
        ".tiff",
        ".pdf",
        ".zip",
        ".gz",
        ".bz2",
        ".xz",
        ".zst",
        ".tar",
        ".7z",
        ".rar",
        ".jar",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".eot",
        ".mp3",
        ".mp4",
        ".mov",
        ".avi",
        ".webm",
        ".wav",
        ".ogg",
        ".so",
        ".dylib",
        ".dll",
        ".exe",
        ".bin",
        ".o",
        ".a",
        ".pyc",
        ".pyo",
        ".whl",
        ".db",
        ".sqlite",
        ".sqlite3",
        ".pkl",
        ".npy",
        ".npz",
        ".parquet",
        ".xlsx",
        ".xls",
        ".docx",
        ".doc",
        ".pptx",
        ".ppt",
    }
)


class BannedTermsUnsetError(RuntimeError):
    """The configured banned-terms/brands list is genuinely UNSET.

    An absent or empty scan list is refused before scanning. The message names
    the offending key so the installation value can be populated.
    """

    @classmethod
    def for_key(cls, key: str, env_var: str | None = None) -> "BannedTermsUnsetError":
        env_hint = f" or supply the ${env_var} JSON secret" if env_var else ""
        return cls(
            f"{key} is unset — configure a registry with scan terms "
            f"(including a nonempty leak list for the tree scan){env_hint}; "
            "refusing to run without a term source."
        )


class BannedTermsUnreadableError(BannedTermsUnsetError):
    """The term list could not be READ — a locked, corrupt, or table-less config store.

    Distinct from a genuinely-unset list so the refusal names a store read failure
    instead of a missing installation value. Both fail closed. A subclass lets
    every ``except BannedTermsUnsetError`` handler catch both cases.
    """

    @classmethod
    def for_store(cls, key: str, env_var: str) -> "BannedTermsUnreadableError":
        return cls(
            f"{key} could not be READ from the config store (locked, corrupt, or missing its "
            f"table) — indistinguishable from an unset list, so the scan fails CLOSED. Retry; "
            f"if it persists, repair the store or supply ${env_var}."
        )


class TreeEnumerationError(RuntimeError):
    """The tracked-file enumeration could not be made, so NOTHING was scanned (#4354).

    Zero files scanned yields zero findings, which the caller previously rendered as
    a clean tree — on the one gate that exists to stop an operator brand reaching a
    PUBLIC repo. A non-repo cwd, a missing ``git``, a ``safe.directory`` refusal and a
    loaded-box ``ls-files`` timeout all produce it, so the non-answer is raised instead
    of returned. The sibling :class:`~teatree.quality.changed_set.ChangedSetError` makes
    the same choice for the same reason.
    """

    @classmethod
    def for_root(cls, repo_root: Path, reason: str) -> "TreeEnumerationError":
        return cls(
            f"could not enumerate the tracked files under {repo_root} ({reason}) — "
            f"nothing was scanned, so the result carries no information about the tree. "
            f"Point --repo-root at a readable git checkout and re-run."
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


def load_brand_terms(db_path: Path | None = None) -> tuple[str, ...]:
    """Load the high-confidence brand list, FAILING LOUD when it is unset.

    The consolidated ``banned_term_registry`` supplies the tree gate's leak
    class. Its environment secret takes precedence over the DB row. An empty
    leak class or unset registry raises
    :class:`BannedTermsUnsetError`: an unset list is too dangerous to scan as
    empty because a load bug would look identical to a deliberate no-brands
    choice.
    """
    from teatree.hooks.banned_term_registry import terms_for_gate  # noqa: PLC0415 — cold-path import

    return terms_for_gate("tree", db_path=db_path)


def scan_text(text: str, terms: tuple[str, ...], allowlist: tuple[str, ...] = ()) -> list[tuple[int, str, str]]:
    """Scan *text* line by line for brand hits; return ``(lineno, term, line)``.

    Routes through the shared :func:`term_match.matched_term`, so the brand
    pass matches the other banned-terms entry points exactly. Empty *terms* is
    a clean no-op.

    *allowlist* is the company-identifier carve-out the other entry points
    already honour; without it an allow-listed identifier (``myorg-engineering``)
    is flagged here for the bare ``myorg`` token with no escape available.
    """
    if not terms:
        return []
    hits: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        term = term_match.matched_term(line, terms, allowlist)
        if term is not None:
            hits.append((lineno, term, line))
    return hits


def git_tracked_files(repo_root: Path) -> list[Path]:
    """Enumerate the git-tracked files under *repo_root* that could hold text.

    Uses ``git ls-files`` (the same source the shell gate's pre-commit
    invocation feeds from) and drops only the suffixes that cannot decode
    (:data:`_BINARY_SUFFIXES`).

    RAISES :class:`TreeEnumerationError` when git could not answer — a non-repo root
    included — and when it answered with no tracked files at all. Both mean the scan
    read nothing, which is a non-answer rather than a clean tree: an unread tree says
    nothing about what is committed in it, so reporting it as a clean scan is the
    fail-open a leak backstop must never take. A repo whose tracked files are all
    binary is a genuine empty RESULT and returns ``[]``.
    """
    try:
        result = run_allowed_to_fail(
            ["git", "-C", str(repo_root), "ls-files", "-z"],
            expected_codes=None,
            timeout=_GIT_LS_TIMEOUT_S,
        )
    except (TimeoutExpired, OSError) as exc:
        raise TreeEnumerationError.for_root(repo_root, f"{type(exc).__name__}: {exc}") from exc
    if result.returncode != 0:
        raise TreeEnumerationError.for_root(repo_root, result.stderr.strip() or f"exit {result.returncode}")
    names = [n for n in result.stdout.split("\0") if n]
    if not names:
        raise TreeEnumerationError.for_root(repo_root, "git reported no tracked files")
    return [repo_root / n for n in names if (repo_root / n).suffix.lower() not in _BINARY_SUFFIXES]


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
            # ``./`` resolves from ``-C`` (a vendored subtree); a bare path resolves from the repo top.
            ["git", "-C", str(repo_root), "show", f"HEAD:./{rel_path}"],
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


def scan_tree(repo_root: Path, terms: tuple[str, ...], allowlist: tuple[str, ...] = ()) -> list[TreeFinding]:
    """Scan every tracked text file for committed brands and conflated terminology.

    Two passes per file, both over the COMMITTED blob (so a working-tree
    edit cannot hide a committed leak): the operator-supplied
    high-confidence brand list (required) and
    the built-in terminology gate (``terminology_gate``), which flags
    teatree-internal vocabulary conflations regardless of any operator
    config.

    *allowlist* carves the company's own identifiers out of the brand pass only;
    the terminology gate is teatree vocabulary, which no operator config exempts.

    Propagates :class:`TreeEnumerationError` — a tree that could not be enumerated
    has no findings for the same reason a tree that was never read has none.
    """
    if not terms:
        message = "banned_term_registry.leak is empty — configure curated leak terms before scanning"
        raise BannedTermsUnsetError(message)
    from teatree.hooks import terminology_gate  # noqa: PLC0415 — deferred: call-time import, kept lazy

    findings: list[TreeFinding] = []
    for path in git_tracked_files(repo_root):
        rel = path.relative_to(repo_root).as_posix()
        text = _scannable_text(repo_root, path, rel)
        if text is None:
            continue
        lines = text.splitlines()
        findings.extend(
            TreeFinding(rel, lineno, term, line) for lineno, term, line in scan_text(text, terms, allowlist)
        )
        if not terminology_gate.path_is_exempt(rel):
            for lineno, finding in terminology_gate.scan_text(text):
                term = f"{finding.phrase} — {finding.correction}"
                findings.append(TreeFinding(rel, lineno, term, lines[lineno - 1]))
    return findings
