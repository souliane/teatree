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

The brand list is a NEW optional high-confidence ``banned_brands`` key
(distinct from the flat ``banned_terms`` the shell gate consumes), read
DB-home from the canonical ``ConfigSetting`` store via the Django-free
:mod:`teatree.config.cold_reader`. The public repo ships with no brands
configured — each operator extends it locally with
``t3 <overlay> config_setting set banned_brands '["...brand..."]'``.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from teatree.config import cold_reader
from teatree.hooks import term_match
from teatree.utils.run import TimeoutExpired, run_allowed_to_fail

# Comma-separated brand list, used by CI where the operator's DB row is not
# populated. Mirrors ``$TEATREE_OVERLAY_LEAK_TERMS`` for the overlay-leak
# gate so the public repo can enforce the backstop from a CI secret
# without committing any brand name. Takes precedence over the DB.
_BRANDS_ENV = "TEATREE_BANNED_BRANDS"
_BRANDS_KEY = "banned_brands"

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
        item_noun = key.rsplit("_", 1)[-1]
        list_label = key.replace("_", "-")
        env_hint = f" (or supply the ${env_var} secret)" if env_var else ""
        return cls(
            f"{key} is unset — set it explicitly (use `{key} = []` if you intend "
            f"no {item_noun}){env_hint}; refusing to run with an unloadable {list_label} list."
        )


class BannedTermsUnreadableError(BannedTermsUnsetError):
    """The term list could not be READ — a locked, corrupt, or table-less config store.

    Distinct from a genuinely-unset list, which a dev/solo box may legitimately warn-and-allow
    on (#3247): an errored read carries no information about what the operator configured, so
    it can only fail CLOSED. A SUBCLASS so every existing ``except BannedTermsUnsetError``
    handler keeps catching it — the two differ only where the warn-and-allow disposition has
    to choose (#4008).
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


def legacy_brand_terms(db_path: Path | None = None) -> tuple[str, ...]:
    """The PRE-registry ``banned_brands`` source: the env secret, else the DB row.

    The registry-free half of :func:`load_brand_terms`, so the registry MIGRATION
    has a source that is genuinely the old config. Reading the dual-read resolver
    there made the rebuild copy the registry back onto itself and its verification
    compare the registry with itself, which passes for any registry at all.
    """
    env = os.environ.get(_BRANDS_ENV, "")
    if env.strip():
        return tuple(t.strip() for t in env.split(",") if t.strip())
    brands = cold_reader.read_setting(_BRANDS_KEY, db_path=db_path)
    if not isinstance(brands, list):
        raise BannedTermsUnsetError.for_key(_BRANDS_KEY, _BRANDS_ENV)
    return tuple(str(t).strip() for t in brands if isinstance(t, str) and t.strip())


def load_brand_terms(db_path: Path | None = None) -> tuple[str, ...]:
    """Load the high-confidence brand list, FAILING LOUD when it is unset.

    ``$TEATREE_BANNED_BRANDS`` (comma-separated) takes precedence so CI feeds
    the list from a secret; a set env var short-circuits before any raise.
    Otherwise the consolidated ``banned_term_registry`` (its ``leak`` class, the
    tree gate's terms) when it is present (dual-read); else the DB-home
    ``banned_brands`` row via the Django-free :mod:`teatree.config.cold_reader`
    (*db_path* overrides the DB path, else the canonical DB / ``T3_CONFIG_DB``).
    An explicit ``banned_brands = []`` is the operator's deliberate no-brands
    choice and returns an empty tuple. A genuinely-unset list — no env, no
    registry, a missing ``banned_brands`` row, or a wrong-typed value — raises
    :class:`BannedTermsUnsetError`: an unset list is too dangerous to scan as
    empty because a load bug would look identical to a deliberate no-brands
    choice.
    """
    if not os.environ.get(_BRANDS_ENV, "").strip():
        from teatree.hooks.banned_term_registry import registry_terms_for_gate  # noqa: PLC0415  dual-read cycle

        registry_terms = registry_terms_for_gate("tree", db_path=db_path)
        if registry_terms is not None:
            return registry_terms
    return legacy_brand_terms(db_path=db_path)


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

    Propagates :class:`TreeEnumerationError` — a tree that could not be enumerated
    has no findings for the same reason a tree that was never read has none.
    """
    from teatree.hooks import terminology_gate  # noqa: PLC0415 — deferred: call-time import, kept lazy

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
