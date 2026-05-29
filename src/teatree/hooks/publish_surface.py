"""Publish-surface classification for the pre-publish gates (#126).

The quote-scanner (#1213) and banned-terms (#1415) gates exist to stop
leaks on PUBLIC surfaces — public-repo issues/PRs, Slack, public REST
posts. A ``git commit`` to a PRIVATE repo is not a public surface: a
private repo's own customer/domain terms are exactly what its commits are
supposed to carry, and hard-blocking them forced an
``--allow-banned-term`` / ``--quote-ok`` override on every commit.

This module classifies a Bash command into one of two surface classes
so the gates can DOWNGRADE from hard-block to warn for the private-repo
commit case ONLY, while leaving every public surface hard-blocked:

``is_git_commit_command`` decides the command is a ``git commit`` — the
one surface eligible for the private-repo carve-out. Public posting
commands (``gh issue create``, ``glab mr note``, ``gh api`` / ``glab
api`` REST, Slack) are never eligible.

``commit_targets_private_repo`` decides the commit's repo (resolved from
the harness ``cwd``) is known-private, via an offline allowlist
(``[teatree] private_repos`` slug substrings in ``~/.teatree.toml``)
first, then a cached ``gh``/``glab`` visibility probe.

Detection is conservative and offline-first: the allowlist needs no
network and is the recommended way to declare private repos; the cached
probe is a best-effort fallback. An unknown/unresolvable repo is treated
as NOT private — the gate stays hard-blocking, never weakened by a
detection failure.

Secrets (API keys, tokens) are blocked on EVERY surface regardless of
the carve-out — see :func:`contains_secret`.
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
# command name — bash applies it to the command's environment. Skipped
# so ``FOO=1 git commit`` is still classified as a ``git commit``.
_ENV_ASSIGNMENT_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")

# ``git commit`` is the first command name + verb (after any env prefix).
_COMMIT_WORD_COUNT: Final[int] = 2

# A slug must have at least ``owner/repo`` (host-prefixed slugs add more).
_MIN_SLUG_PARTS: Final[int] = 2

# How long a cached visibility verdict stays fresh. Repo visibility
# changes rarely; a day-long cache keeps the offline path fast while
# tolerating the occasional flip.
_VISIBILITY_TTL_S: Final[int] = 24 * 60 * 60

# Visibility probe budget — a hook that hangs blocks the user, so the
# network call gets a tight timeout and any failure falls back to
# "unknown" (treated as NOT private).
_PROBE_TIMEOUT_S: Final[int] = 5


def is_git_commit_command(command: str) -> bool:
    """Return True iff the first command segment is a ``git commit``.

    The private-repo carve-out applies ONLY to ``git commit`` — it is the
    single publish surface that writes to a repo rather than to a public
    posting surface. ``gh``/``glab``/``curl``/Slack publishes and the
    ``gh api`` / ``glab api`` REST paths are never eligible.

    A leading inline env assignment (``FOO=1 git commit``) is skipped so
    the command name resolves to ``git``.
    """
    words = first_segment_words(command)
    while words and _ENV_ASSIGNMENT_RE.fullmatch(words[0]):
        words = words[1:]
    return len(words) >= _COMMIT_WORD_COUNT and words[0] == "git" and words[1] == "commit"


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


def _visibility_cache_path() -> Path:
    base = os.environ.get("T3_DATA_DIR")
    root = Path(base) if base else Path.home() / ".teatree"
    return root / "repo-visibility-cache.json"


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
    errors. ``None`` is the fail-safe "unknown" — the caller then treats
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
    try:
        result = run_allowed_to_fail(
            ["glab", "api", f"projects/{repo_path.replace('/', '%2F')}", "--jq", ".visibility"],
            expected_codes=(0,),
            timeout=_PROBE_TIMEOUT_S,
        )
    except (CommandFailedError, OSError):
        return None
    verdict = result.stdout.strip().upper()
    return verdict or None


def _slug_is_private(slug: str) -> bool:
    """Resolve whether ``slug`` is a private repo (cache → probe → cache write)."""
    cached = _read_visibility_cache(slug)
    if cached is not None:
        return cached == "PRIVATE"
    verdict = _probe_visibility(slug)
    if verdict is None:
        return False
    _write_visibility_cache(slug, verdict)
    return verdict == "PRIVATE"


def commit_targets_private_repo(cwd: Path | None, *, config_path: Path | None = None) -> bool:
    """Return True iff a commit in ``cwd`` targets a known-private repo.

    Offline-first: the ``[teatree] private_repos`` slug-substring
    allowlist is consulted before any network probe, so a fully-offline
    session still gets the carve-out for declared repos. The cached
    ``gh``/``glab`` visibility probe is the fallback. An unresolvable repo
    is NOT private (the gate stays hard-blocking) — detection failure
    never weakens the gate.
    """
    if cwd is None:
        return False
    slug = _slug_for_cwd(cwd)
    if not slug:
        return False
    lowered = slug.lower()
    for entry in _private_repo_allowlist(config_path):
        if entry in lowered:
            return True
    return _slug_is_private(slug)


# ── Always-on secret detection ──────────────────────────────────────

# High-confidence secret shapes. These are blocked on EVERY surface,
# including a private-repo commit — the carve-out is about a repo's own
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

    The private-repo carve-out (#126) applies when ALL hold: the tool is
    ``Bash`` (the only surface a commit reaches); the command is a ``git
    commit`` (public posting surfaces excluded); the commit targets a
    known-private repo (offline allowlist first, cached visibility probe
    second); the payload was actually resolved (the fail-closed sentinel
    means the scanner could not read the body, so it must still hard-
    block); and the payload carries no high-confidence secret (credentials
    leak on every surface, private repos included).

    Any other surface (public-repo issue/PR, ``gh api`` / ``glab api``
    REST, Slack) returns ``False`` so the gate stays hard-blocking.
    """
    if tool_name != "Bash":
        return False
    if not is_git_commit_command(command):
        return False
    from teatree.hooks._command_parser import is_fail_closed_sentinel  # noqa: PLC0415

    if is_fail_closed_sentinel(payload):
        return False
    if contains_secret(payload):
        return False
    return commit_targets_private_repo(cwd, config_path=config_path)
