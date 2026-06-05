"""Repo-visibility / privacy resolution for the publish-surface carve-out.

Split out of :mod:`teatree.hooks.publish_surface` to keep that module under
the project's per-file LOC ceiling. This module owns the "is this repo
private?" question and nothing about command classification:

- the offline ``[teatree] private_repos`` slug-substring allowlist (the
    reliable, network-free, recommended mechanism),
- the day-cached ``gh``/``glab`` live-visibility probe (best-effort
    fallback; the binary is resolved against an augmented PATH so it works
    inside the restricted PreToolUse subprocess), and
- the slug resolution from a repo ``cwd``.

Detection is conservative and offline-first; an unknown/unresolvable repo is
treated as NOT private so a detection failure never weakens the gate.
"""

import json
import os
import shutil
import time
from pathlib import Path
from typing import Final, TypedDict

from teatree.utils import git
from teatree.utils.run import CommandFailedError, run_allowed_to_fail


class _VisibilityEntry(TypedDict):
    """One cached repo-visibility verdict with its capture timestamp."""

    ts: float
    visibility: str


# A slug must have at least ``owner/repo`` (host-prefixed slugs add more).
_MIN_SLUG_PARTS: Final[int] = 2

# How long a cached visibility verdict stays fresh. Repo visibility changes
# rarely; a day-long cache keeps the offline path fast.
_VISIBILITY_TTL_S: Final[int] = 24 * 60 * 60

# Visibility probe budget -- a hook that hangs blocks the user, so the
# network call gets a tight timeout and any failure falls back to "unknown".
_PROBE_TIMEOUT_S: Final[int] = 5

# The PreToolUse hook subprocess inherits a restricted PATH, so a bare
# ``gh``/``glab`` may not resolve even though it is installed. The probe
# augments PATH with the common install locations before resolving the tool,
# so the live-visibility fallback works in-hook instead of silently failing
# to "unknown" and over-blocking the user's own private repo.
_PROBE_PATH_EXTRA: Final[tuple[str, ...]] = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    str(Path.home() / ".local" / "bin"),
)


def _config_path() -> Path:
    override = os.environ.get("T3_BANNED_TERMS_CONFIG")
    if override:
        return Path(override)
    return Path.home() / ".teatree.toml"


def _private_repo_allowlist(config_path: Path | None = None) -> list[str]:
    """Return the ``[teatree] private_repos`` slug-substring allowlist.

    Each entry is matched as a case-insensitive substring against the repo's
    ``origin`` slug (``host/owner/repo``), so a single organisation-namespace
    entry covers every repo under that namespace. Reads the TOML directly (no
    Django/config import) to stay importable from the hook process.
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


def slug_for_cwd(cwd: Path) -> str:
    """Return the ``origin`` slug (``host/owner/repo``) for ``cwd``, or ``""``.

    The full slug (including host) is used so an organisation-namespace
    allowlist entry matches a GitLab remote and a GitHub probe can be keyed by
    the same string.
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

    The historical default ``~/.teatree`` is the shell-sourceable config FILE
    in this environment, so a cache write under it raised "Not a directory"
    and the verdict could never persist. Honour ``T3_DATA_DIR`` when set, else
    use the XDG cache dir. If the chosen root already exists as a
    non-directory, fall back to a sibling so the write still succeeds.
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


def _probe_search_path() -> str:
    """Return ``$PATH`` augmented with the common ``gh``/``glab`` install dirs."""
    return os.pathsep.join([os.environ.get("PATH", ""), *_PROBE_PATH_EXTRA])


def _resolve_probe_tool(tool: str) -> str | None:
    """Resolve ``tool`` against the augmented probe PATH, or ``None``.

    The PreToolUse hook subprocess inherits a restricted PATH where a bare
    ``gh``/``glab`` may not resolve; resolving against the augmented path lets
    the live-visibility fallback work in-hook instead of over-blocking.
    """
    return shutil.which(tool, path=_probe_search_path())


def _probe_env() -> dict[str, str]:
    """Return the process environment with the augmented probe PATH."""
    return {**os.environ, "PATH": _probe_search_path()}


def _probe_gh(repo_path: str) -> str | None:
    binary = _resolve_probe_tool("gh")
    if binary is None:
        return None
    try:
        result = run_allowed_to_fail(
            [binary, "repo", "view", repo_path, "--json", "visibility", "--jq", ".visibility"],
            expected_codes=(0,),
            env=_probe_env(),
            timeout=_PROBE_TIMEOUT_S,
        )
    except (CommandFailedError, OSError):
        return None
    verdict = result.stdout.strip().upper()
    return verdict or None


def _probe_glab(repo_path: str) -> str | None:
    # ``glab api`` has no ``--jq`` flag (unlike ``gh``), so the verdict is
    # parsed from the full project JSON in Python. Passing ``--jq`` makes glab
    # exit non-zero with "Unknown flag", silently defeating the carve-out for
    # every GitLab repo.
    binary = _resolve_probe_tool("glab")
    if binary is None:
        return None
    try:
        result = run_allowed_to_fail(
            [binary, "api", f"projects/{repo_path.replace('/', '%2F')}"],
            expected_codes=(0,),
            env=_probe_env(),
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


def probe_visibility(slug: str) -> str | None:
    """Probe repo visibility via ``gh`` (GitHub) or ``glab`` (GitLab).

    Returns ``"PRIVATE"`` / ``"PUBLIC"`` (upper-cased) or ``None`` when the
    tool is unavailable, the slug is unrecognised, or the probe errors.
    ``None`` is the fail-safe "unknown" -- the caller then treats the repo as
    NOT private and the gate stays hard-blocking.
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


def slug_is_private(slug: str) -> bool:
    """Resolve whether ``slug`` is a private repo (cache -> probe -> cache write)."""
    cached = _read_visibility_cache(slug)
    if cached is not None:
        return cached == "PRIVATE"
    verdict = probe_visibility(slug)
    if verdict is None:
        return False
    _write_visibility_cache(slug, verdict)
    return verdict == "PRIVATE"


def slug_is_allowlisted_private(slug: str, config_path: Path | None) -> bool:
    """Return True iff ``slug`` matches the offline allowlist."""
    lowered = slug.lower()
    return any(entry in lowered for entry in _private_repo_allowlist(config_path))


def term_is_own_repo_slug(term: str, config_path: Path | None = None) -> bool:
    """Return True iff ``term`` is (a token-run of) a ``[teatree] private_repos`` entry.

    A configured ``private_repos`` entry is, by definition, a private repo's
    OWN org/repo slug substring (a neutral example: ``acme-engineering``). When
    such an entry is the banned term a commit message tripped on, the match is
    the repo naming ITSELF -- the work-item URL ``host/<org>/<repo>/...`` -- not
    a foreign customer leak, so it is downgrade-eligible on that repo's own
    commits.

    The match is token-CONTAINMENT: the term's tokens must appear as a
    CONTIGUOUS run within an allowlist entry's tokens. A term equal to the entry
    qualifies (``acme-engineering``), AND so does a token-run of it -- the org
    prefix ``acme`` of ``acme-engineering`` (#1958). A work-item URL
    ``host/acme-engineering/.../-/issues/N`` tokenizes that prefix out of the
    repo's OWN identity, so the banned-terms scanner reports the prefix token,
    not the whole slug; the prefix is still the repo naming itself, not a foreign
    leak. A FOREIGN term is not a run of any entry, and a SUPERSET term (a longer
    slug that merely starts with the entry, e.g. ``acme-engineering-services``)
    is longer than the entry's token run and so is NOT contained -- both stay
    blocked.
    """
    from teatree.hooks.term_match import _contains_run, tokens  # noqa: PLC0415

    term_tokens = tokens(term)
    if not term_tokens:
        return False
    return any(_contains_run(tokens(entry), term_tokens) for entry in _private_repo_allowlist(config_path))
