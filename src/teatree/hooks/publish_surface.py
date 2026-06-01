"""Publish-surface classification for the pre-publish gates (#126).

The quote-scanner (#1213) and banned-terms (#1415) gates exist to stop
leaks on PUBLIC surfaces -- public-repo issues/PRs, Slack, public REST
posts. A ``git commit`` to a PRIVATE repo is not a public surface: a
private repo's own customer/domain terms are exactly what its commits are
supposed to carry, and hard-blocking them forced an
``--allow-banned-term`` / ``--quote-ok`` override on every commit.

This module classifies a Bash command into one of two surface classes
so the gates can DOWNGRADE from hard-block to warn for the private-repo
commit case ONLY, while leaving every public surface hard-blocked:

``is_git_commit_command`` decides the command is a ``git commit`` -- the
one surface eligible for the private-repo carve-out.

``is_gh_glab_posting_command`` decides the command is a structured
``gh``/``glab`` PR/issue create-or-comment command (NOT ``gh api`` /
``glab api`` raw REST, NOT ``curl``/Slack) that posts to a specific
repo target. These are eligible for the carve-out ONLY when the target
repo is POSITIVELY known-private (resolved from ``--repo``/``-R`` flag
first, then CWD fallback). Unknown or public targets stay hard-blocked.

``commit_targets_private_repo`` decides the commit's repo is known-private,
via an offline allowlist (``[teatree] private_repos`` slug substrings in
``~/.teatree.toml``) first, then a cached ``gh``/``glab`` visibility probe.
The dir it resolves is the one whose repo the commit LANDS in (via
:func:`effective_repo_dir`): the ``--git-dir`` repo if specified, else the
repo discovered from the ``-C``-adjusted working directory, falling back to
the harness ``cwd`` for a plain ``git commit``. ``--work-tree`` only sets
the working tree and NEVER selects the repo, so it is excluded -- a
``--git-dir <PUBLIC> --work-tree <PRIVATE>`` commit lands in the PUBLIC
repo, and resolving the private work-tree would leak banned content to
public history. A sub-agent's ``git -C <worktree> commit`` runs from an
ambient hook cwd that has reset away from the worktree, so resolving from
the command's own flag is what keeps the carve-out from over-blocking that
commit.

``posting_command_targets_private_repo`` applies the same privacy
decision to a ``gh``/``glab`` posting command: the target repo slug is
extracted from ``--repo``/``-R`` in the command first; if absent, the
CWD repo is used as a fallback. Unknown/unresolvable => NOT private.

Detection is conservative and offline-first: the allowlist needs no
network and is the recommended way to declare private repos; the cached
probe is a best-effort fallback. An unknown/unresolvable repo is treated
as NOT private -- the gate stays hard-blocking, never weakened by a
detection failure.

Secrets (API keys, tokens) are blocked on EVERY surface regardless of
the carve-out -- see :func:`contains_secret`.

The companion :mod:`teatree.hooks.publish_destination` reuses the
repo-target helpers here (``_extract_repo_flag``, ``_slug_for_cwd``,
``_config_path``, the eligible-verb sets) to make the banned-terms /
bare-reference gates DESTINATION-AWARE: those gates scan only PUBLIC
targets and skip a publish whose destination is provably internal.
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Final, TypedDict

from teatree.hooks._command_parser import first_segment_words
from teatree.utils import git
from teatree.utils.run import CommandFailedError, run_allowed_to_fail


class _VisibilityEntry(TypedDict):
    """One cached repo-visibility verdict with its capture timestamp."""

    ts: float
    visibility: str


# A leading ``KEY=value`` token is an inline env assignment, not the
# command name -- bash applies it to the command's environment. Skipped
# so ``FOO=1 git commit`` is still classified as a ``git commit``.
_ENV_ASSIGNMENT_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")

# ``git commit`` is the first command name + verb (after any env prefix).
_COMMIT_WORD_COUNT: Final[int] = 2

# Value-taking global ``git`` flags that sit BEFORE the sub-command verb:
# ``-C <dir>``, ``--git-dir <dir>``, ``--work-tree <dir>``. The verb-finding
# walk skips them as flag(+value) pairs so ``git --work-tree=x commit`` and
# ``git --git-dir=x commit`` are still recognised as commits. These three
# are recognised for VERB SKIPPING only -- repo identity is resolved
# separately (git-dir/-C only, never --work-tree) by ``effective_repo_dir``.
_GIT_GLOBAL_DIR_FLAGS: Final[frozenset[str]] = frozenset({"-C", "--git-dir", "--work-tree"})

# A slug must have at least ``owner/repo`` (host-prefixed slugs add more).
_MIN_SLUG_PARTS: Final[int] = 2

# How long a cached visibility verdict stays fresh. Repo visibility
# changes rarely; a day-long cache keeps the offline path fast while
# tolerating the occasional flip.
_VISIBILITY_TTL_S: Final[int] = 24 * 60 * 60

# Visibility probe budget -- a hook that hangs blocks the user, so the
# network call gets a tight timeout and any failure falls back to
# "unknown" (treated as NOT private).
_PROBE_TIMEOUT_S: Final[int] = 5

# Eligible ``gh`` sub-command pairs: (tool, verb) where "tool" is the
# second word (pr/issue) and "verb" is the third word (create/comment).
# ``gh api`` is NOT in this set -- raw REST can target arbitrary surfaces.
_GH_ELIGIBLE_VERBS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("pr", "create"),
        ("pr", "comment"),
        ("issue", "create"),
        ("issue", "comment"),
    }
)

# Eligible ``glab`` sub-command pairs. ``glab api`` is NOT in this set.
_GLAB_ELIGIBLE_VERBS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("mr", "create"),
        ("mr", "note"),
        ("issue", "create"),
        ("issue", "note"),
    }
)


def _strip_git_global_prefix(words: list[str]) -> list[str]:
    """Drop a leading env assignment and ``git`` global worktree flags.

    Leaves ``words`` positioned so ``words[0]`` is the sub-command verb of
    a ``git`` invocation. A leading inline env assignment (``FOO=1 git``)
    and the value-taking global flags (``-C <dir>``, ``--git-dir <dir>``,
    ``--work-tree <dir>``, plus their ``=`` forms) are skipped so
    ``git -C <dir> commit`` resolves to the ``commit`` verb. ``words[0]``
    must already be ``git`` for the global-flag skip to run.
    """
    while words and _ENV_ASSIGNMENT_RE.fullmatch(words[0]):
        words = words[1:]
    if not words or words[0] != "git":
        return words
    rest = words[1:]
    i = 0
    while i < len(rest):
        w = rest[i]
        if w in _GIT_GLOBAL_DIR_FLAGS:
            i += 2
            continue
        if any(w.startswith(flag + "=") for flag in _GIT_GLOBAL_DIR_FLAGS):
            i += 1
            continue
        break
    return ["git", *rest[i:]]


def is_git_commit_command(command: str) -> bool:
    """Return True iff the first command segment is a ``git commit``.

    A leading inline env assignment (``FOO=1 git commit``) and ``git``
    global worktree flags (``git -C <dir> commit``, ``--git-dir``,
    ``--work-tree``) are skipped so the command still resolves to the
    ``commit`` verb.
    """
    words = _strip_git_global_prefix(first_segment_words(command))
    return len(words) >= _COMMIT_WORD_COUNT and words[0] == "git" and words[1] == "commit"


def _last_flag_value(words: list[str], flag: str) -> str | None:
    """Return the LAST ``flag <value>`` / ``flag=<value>`` value, or ``None``.

    ``git`` resolves a repeated global flag LAST-WINS, so this scans the
    whole word list and keeps the final occurrence across both the
    space-separated and ``=`` spellings.
    """
    found: str | None = None
    i = 0
    prefix = flag + "="
    while i < len(words):
        w = words[i]
        if w == flag and i + 1 < len(words):
            found = words[i + 1]
            i += 2
            continue
        if w.startswith(prefix):
            found = w[len(prefix) :]
        i += 1
    return found


def effective_repo_dir(command: str) -> str | None:
    """Return the dir whose repo a ``git`` command's commit LANDS in, or ``None``.

    ``git`` selects a commit's repo as: the ``--git-dir``/``$GIT_DIR`` repo
    if specified, otherwise the repo discovered from the effective working
    directory, which ``-C <dir>`` changes. ``--work-tree`` only sets the
    working tree and NEVER selects the repo, so it is excluded here -- a
    ``--git-dir <PUBLIC> --work-tree <PRIVATE>`` commit lands in the PUBLIC
    repo, and resolving the private work-tree would leak banned content to
    public history. Repeated ``-C``/``--git-dir`` flags are LAST-WINS.

    Resolution: ``--git-dir`` (last-wins) if present, resolved relative to
    the ``-C``-adjusted cwd when relative; else the ``-C``-adjusted path
    (last-wins ``-C``). ``None`` when neither flag is present, so the caller
    falls back to the ambient cwd for a plain ``git commit``.
    """
    words = first_segment_words(command)
    dash_c = _last_flag_value(words, "-C")
    git_dir = _last_flag_value(words, "--git-dir")
    if git_dir is not None:
        if dash_c is not None and not Path(git_dir).is_absolute():
            return str(Path(dash_c) / git_dir)
        return git_dir
    return dash_c


def is_gh_glab_posting_command(command: str) -> bool:
    """Return True iff the first command segment is an eligible ``gh``/``glab`` posting verb.

    Eligible: ``gh pr create``, ``gh pr comment``, ``gh issue create``,
    ``gh issue comment``, ``glab mr create``, ``glab mr note``,
    ``glab issue create``, ``glab issue note``.

    NOT eligible: ``gh api`` / ``glab api`` (raw REST -- can target any
    surface), ``gh repo view``, ``glab mr list``, or anything that is not
    a structured create-or-comment verb against a single repo target.

    The carve-out uses this to gate which posting commands may be
    downgraded from hard-block to warn when the target repo is positively
    known-private.
    """
    words = first_segment_words(command)
    if len(words) < 3:  # noqa: PLR2004
        return False
    tool, sub, verb = words[0], words[1], words[2]
    if tool == "gh":
        return (sub, verb) in _GH_ELIGIBLE_VERBS
    if tool == "glab":
        return (sub, verb) in _GLAB_ELIGIBLE_VERBS
    return False


def _config_path() -> Path:
    override = os.environ.get("T3_BANNED_TERMS_CONFIG")
    if override:
        return Path(override)
    return Path.home() / ".teatree.toml"


def _private_repo_allowlist(config_path: Path | None = None) -> list[str]:
    """Return the ``[teatree] private_repos`` slug-substring allowlist.

    Each entry is matched as a case-insensitive substring against the
    repo's ``origin`` slug (``host/owner/repo``), so a single
    organisation-namespace entry covers every repo under that namespace.
    Reads the TOML directly (no Django/config import) to stay importable
    from the hook process without a full settings bootstrap.
    """
    import tomllib  # noqa: PLC0415

    target = config_path if config_path is not None else _config_path()
    if not target.is_file():
        return []
    try:
        raw = tomllib.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    teatree = raw.get("teatree", {})
    if not isinstance(teatree, dict):
        return []
    entries = teatree.get("private_repos", [])
    if not isinstance(entries, list):
        return []
    return [str(e).strip().lower() for e in entries if str(e).strip()]


def _slug_for_cwd(cwd: Path) -> str:
    """Return the ``origin`` slug (``host/owner/repo``) for ``cwd``, or ``""``.

    The full slug (including host) is used so an organisation-namespace
    allowlist entry matches a GitLab remote and a GitHub probe can be
    keyed by the same string.
    """
    try:
        url = git.remote_url(repo=str(cwd))
    except CommandFailedError:
        return ""
    if not url:
        return ""
    cleaned = url.strip().rstrip("/").removesuffix(".git")
    if "://" in cleaned:
        return cleaned.split("://", 1)[1]
    if "@" in cleaned and ":" in cleaned:
        host, _, path = cleaned.partition(":")
        host = host.rsplit("@", 1)[-1]
        return f"{host}/{path}"
    return cleaned


def _cache_root() -> Path:
    """Resolve a writable cache dir that never collides with the config file.

    The historical default ``~/.teatree`` is the shell-sourceable config
    FILE in this environment, so a cache write under it raised "Not a
    directory" and the verdict could never persist -- every commit re-probed.
    Honour ``T3_DATA_DIR`` when set, else use the XDG cache dir (matching
    ``url_title_fetcher``'s ``~/.cache/teatree``). If the chosen root already
    exists as a non-directory, fall back to a sibling so the write still
    succeeds rather than being silently swallowed as an ``OSError``.
    """
    base = os.environ.get("T3_DATA_DIR")
    if base:
        return Path(base)
    xdg = os.environ.get("XDG_CACHE_HOME")
    root = (Path(xdg) if xdg else Path.home() / ".cache") / "teatree"
    if root.exists() and not root.is_dir():
        return Path.home() / ".teatree-data"
    return root


def _visibility_cache_path() -> Path:
    return _cache_root() / "repo-visibility-cache.json"


def _read_visibility_cache(slug: str) -> str | None:
    """Return a fresh cached visibility verdict for ``slug``, or ``None``."""
    path = _visibility_cache_path()
    if not path.is_file():
        return None
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entry = cache.get(slug) if isinstance(cache, dict) else None
    if not isinstance(entry, dict):
        return None
    ts = entry.get("ts")
    verdict = entry.get("visibility")
    if not isinstance(ts, (int, float)) or not isinstance(verdict, str):
        return None
    if time.time() - ts > _VISIBILITY_TTL_S:
        return None
    return verdict


def _write_visibility_cache(slug: str, verdict: str) -> None:
    """Persist a visibility verdict for ``slug`` (best-effort)."""
    path = _visibility_cache_path()
    cache: dict[str, _VisibilityEntry] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cache = loaded
        except (OSError, ValueError):
            cache = {}
    cache[slug] = _VisibilityEntry(ts=time.time(), visibility=verdict)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
        return


def _probe_visibility(slug: str) -> str | None:
    """Probe repo visibility via ``gh`` (GitHub) or ``glab`` (GitLab).

    Returns ``"PRIVATE"`` / ``"PUBLIC"`` (upper-cased) or ``None`` when
    the tool is unavailable, the slug is unrecognised, or the probe
    errors. ``None`` is the fail-safe "unknown" -- the caller then treats
    the repo as NOT private and the gate stays hard-blocking.
    """
    parts = slug.split("/")
    if len(parts) < _MIN_SLUG_PARTS:
        return None
    host = parts[0] if "." in parts[0] else ""
    repo_path = "/".join(parts[1:]) if host else slug
    if host.startswith("gitlab"):
        return _probe_glab(repo_path)
    if host.startswith("github") or not host:
        return _probe_gh(repo_path)
    return None


def _probe_gh(repo_path: str) -> str | None:
    try:
        result = run_allowed_to_fail(
            ["gh", "repo", "view", repo_path, "--json", "visibility", "--jq", ".visibility"],
            expected_codes=(0,),
            timeout=_PROBE_TIMEOUT_S,
        )
    except (CommandFailedError, OSError):
        return None
    verdict = result.stdout.strip().upper()
    return verdict or None


def _probe_glab(repo_path: str) -> str | None:
    # ``glab api`` has no ``--jq`` flag (unlike ``gh``), so the verdict is
    # parsed from the full project JSON in Python. Passing ``--jq`` makes
    # glab exit non-zero with "Unknown flag", which silently defeats the
    # private-repo carve-out for every GitLab repo.
    try:
        result = run_allowed_to_fail(
            ["glab", "api", f"projects/{repo_path.replace('/', '%2F')}"],
            expected_codes=(0,),
            timeout=_PROBE_TIMEOUT_S,
        )
    except (CommandFailedError, OSError):
        return None
    try:
        project = json.loads(result.stdout)
    except ValueError:
        return None
    visibility = project.get("visibility") if isinstance(project, dict) else None
    if not isinstance(visibility, str):
        return None
    return visibility.strip().upper() or None


def _slug_is_private(slug: str) -> bool:
    """Resolve whether ``slug`` is a private repo (cache -> probe -> cache write)."""
    cached = _read_visibility_cache(slug)
    if cached is not None:
        return cached == "PRIVATE"
    verdict = _probe_visibility(slug)
    if verdict is None:
        return False
    _write_visibility_cache(slug, verdict)
    return verdict == "PRIVATE"


def _slug_is_allowlisted_private(slug: str, config_path: Path | None) -> bool:
    """Return True iff ``slug`` matches the offline allowlist."""
    lowered = slug.lower()
    return any(entry in lowered for entry in _private_repo_allowlist(config_path))


def commit_targets_private_repo(cwd: Path | None, *, config_path: Path | None = None) -> bool:
    """Return True iff a commit in ``cwd`` targets a known-private repo.

    Offline-first: the ``[teatree] private_repos`` slug-substring
    allowlist is consulted before any network probe, so a fully-offline
    session still gets the carve-out for declared repos. The cached
    ``gh``/``glab`` visibility probe is the fallback. An unresolvable repo
    is NOT private (the gate stays hard-blocking) -- detection failure
    never weakens the gate.
    """
    if cwd is None:
        return False
    slug = _slug_for_cwd(cwd)
    if not slug:
        return False
    if _slug_is_allowlisted_private(slug, config_path):
        return True
    return _slug_is_private(slug)


def _extract_repo_flag(words: list[str]) -> str:
    """Extract the EFFECTIVE ``--repo``/``-R`` value, or return ``""``.

    ``gh`` and ``glab`` resolve a repeated ``--repo``/``-R`` flag LAST-WINS
    (the same effective-resolution rule as ``-X GET -X POST`` for the HTTP
    method). Reading the FIRST match would let a crafted command claim a
    private slug while the tool actually posts to a trailing PUBLIC slug --
    a leak that defeats the carve-out's load-bearing safety property. So
    this scans the WHOLE word list and keeps the LAST occurrence.

    All four forms are recognised and the last one anywhere wins regardless
    of form: ``--repo X``, ``--repo=X``, ``-R X``, ``-R=X``.
    """
    found = ""
    i = 0
    while i < len(words):
        w = words[i]
        if w in {"--repo", "-R"} and i + 1 < len(words):
            found = words[i + 1]
            i += 2
            continue
        if w.startswith("--repo="):
            found = w[len("--repo=") :]
        elif w.startswith("-R="):
            found = w[len("-R=") :]
        i += 1
    return found


def posting_command_targets_private_repo(
    command: str,
    cwd: Path | None,
    *,
    config_path: Path | None = None,
) -> bool:
    """Return True iff the gh/glab posting command's target repo is known-private.

    Resolves the target repo slug, mirroring how ``gh``/``glab`` themselves
    resolve their target, in priority order:

    - ``--repo``/``-R`` from the command (explicit flag always wins).
    - For ``gh`` ONLY: the ``GH_REPO`` env var, when no flag is present.
        ``gh`` reads ``GH_REPO`` as its default target, so a flagless
        ``gh pr create`` with ``GH_REPO`` exported posts there -- NOT to the
        CWD repo. The hook shares the process environment ``gh`` inherits, so
        ``os.environ`` reflects the same value. ``glab`` has no equivalent
        env var, so this step is skipped for it.
    - The CWD origin slug, as the final fallback.

    An explicit ``--repo owner/name`` slug has no host prefix; it is matched
    against the allowlist as-is, then passed to the visibility probe directly
    (``gh`` probe for GitHub slugs, ``glab`` probe requires the host to detect
    GitLab; a bare ``owner/name`` defaults to the GitHub probe path).

    Unknown/unresolvable target => NOT private (default-deny preserved).
    """
    words = first_segment_words(command)
    explicit_repo = _extract_repo_flag(words)
    is_gh = bool(words) and words[0] == "gh"

    if explicit_repo:
        slug = explicit_repo
    elif is_gh and os.environ.get("GH_REPO", ""):
        slug = os.environ["GH_REPO"]
    elif cwd is not None:
        slug = _slug_for_cwd(cwd)
    else:
        return False

    if not slug:
        return False

    if _slug_is_allowlisted_private(slug, config_path):
        return True
    return _slug_is_private(slug)


# -- Always-on secret detection -----------------------------------------------

# High-confidence secret shapes. These are blocked on EVERY surface,
# including a private-repo commit -- the carve-out is about a repo's own
# domain words, never about leaking a live credential into git history.
# The patterns are intentionally narrow (recognisable provider prefixes
# + length) to avoid false positives on ordinary prose.
_SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    # GitHub personal-access / fine-grained / OAuth / app tokens.
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,}\b"),
    # GitLab personal/project/deploy tokens.
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    # Slack bot/user/app tokens.
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    # AWS access key id.
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    # Google API key.
    re.compile(r"\bAIza[A-Za-z0-9_-]{35}\b"),
    # OpenAI / Anthropic style secret keys.
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk-ant-[A-Za-z0-9-]{20,}\b"),
    # PEM private-key block header.
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"),
)


def contains_secret(text: str) -> bool:
    """Return True iff ``text`` carries a high-confidence secret shape.

    Used by both gates to keep secrets hard-blocked even on a private-repo
    commit that is otherwise eligible for the domain-word carve-out.
    """
    if not text:
        return False
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)


def carve_out_applies(
    tool_name: str,
    command: str,
    payload: str,
    cwd: Path | None,
    *,
    config_path: Path | None = None,
) -> bool:
    """Return True iff a HIGH/banned match on ``payload`` should DOWNGRADE.

    The private-repo carve-out applies when ALL hold:

    - The tool is ``Bash``.
    - The payload was actually resolved (fail-closed sentinel => hard-block).
    - The payload carries no high-confidence secret (credentials always leak).
    - The command is a ``git commit`` to a known-private repo (resolved
        from the dir whose repo the commit LANDS in -- ``--git-dir`` else the
        ``-C``-adjusted cwd, never ``--work-tree`` -- when present, else the
        CWD), OR a structured ``gh``/``glab`` create-or-comment command whose
        RESOLVED TARGET is positively known-private (--repo/-R first, CWD
        fallback).

    Ineligible regardless: ``gh api`` / ``glab api`` raw REST, ``curl``,
    Slack, and any non-structured verb. Public/unknown targets stay blocked.
    """
    if tool_name != "Bash":
        return False
    from teatree.hooks._command_parser import is_fail_closed_sentinel  # noqa: PLC0415

    if is_fail_closed_sentinel(payload):
        return False
    if contains_secret(payload):
        return False

    if is_git_commit_command(command):
        repo_dir = effective_repo_dir(command)
        target = Path(repo_dir) if repo_dir else cwd
        return commit_targets_private_repo(target, config_path=config_path)

    if is_gh_glab_posting_command(command):
        return posting_command_targets_private_repo(command, cwd, config_path=config_path)

    return False
