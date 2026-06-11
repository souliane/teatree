"""Repo-visibility / privacy resolution for the publish-surface carve-out.

Split out of :mod:`teatree.hooks.publish_surface` to keep that module under
the project's per-file LOC ceiling. This module owns the "is this repo
private?" question and nothing about command classification:

- the offline ``[teatree] private_repos`` slug-namespace allowlist (the
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
    """Return the ``[teatree] private_repos`` slug-namespace allowlist.

    Each entry is matched as a case-insensitive path-segment prefix against the
    repo's host-stripped ``owner/repo`` slug (see
    :func:`slug_namespace_matches`), so a single organisation-namespace entry
    covers every repo under that namespace. Entries may be written bare
    (``owner/repo``) or host-qualified (``host/owner/repo`` -- the form a repo
    URL carries); the match is host-qualification-symmetric, so either form
    covers the commit surface (host-qualified cwd slug) and the pr-create
    surface (bare ``--repo`` slug) alike. Reads the TOML directly (no
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

    Remote forms normalize to a canonical slug:

    - ``https://host/owner/repo`` -> ``host/owner/repo`` (host kept),
    - ``user@host:owner/repo`` (SCP-style SSH) -> ``host/owner/repo`` ONLY when
        ``host`` is a CANONICAL hostname (it contains a dot, e.g.
        ``git@gitlab.com:org/repo`` -> ``gitlab.com/org/repo``). When ``host`` is
        a dotless SSH CONFIG ALIAS (``git@gh-acct:owner/repo``, the ``Host
        gh-acct`` form from ``~/.ssh/config`` that maps to a real ``HostName``),
        the ``user@<alias>`` prefix is DROPPED -> ``owner/repo``: the alias is a
        LOCAL name with no canonical identity, so keeping it glued ``gh-acct`` in
        as the leading slug segment, where the dot-keyed host-strip / visibility
        probe could not recognise it and a private own-repo failed to downgrade
        (#1415). A dotted alias (``github.com-acct``) is kept as the host
        segment but the downstream :func:`_strip_host_prefix` / probe already
        strip a dotted leading segment, so it resolves correctly either way.
    - ``alias:owner/repo`` (SSH config ``Host alias``, no ``user@``) ->
        ``owner/repo`` -- same rationale: a local alias with no canonical
        identity is DROPPED. Keeping it (the old verbatim return) glued the alias
        into the slug, and an alias whose name contained an allowlist entry then
        tripped the substring matcher and falsely downgraded a PUBLIC repo
        (#1953).

    ``FileNotFoundError`` (the ``git`` binary unresolved on the restricted hook
    PATH) and ``OSError`` are caught alongside ``CommandFailedError`` so a
    degraded subprocess fails SAFE to an empty slug -- an uncaught error would
    propagate out of the carve-out and crash the whole gate, denying the offline
    allowlist any chance to downgrade.
    """
    try:
        url = git.remote_url(repo=str(cwd))
    except (CommandFailedError, OSError):
        return ""
    if not url:
        return ""
    cleaned = url.strip().rstrip("/").removesuffix(".git")
    if "://" in cleaned:
        return cleaned.split("://", 1)[1]
    if ":" in cleaned and "/" not in cleaned.partition(":")[0]:
        host, _, path = cleaned.partition(":")
        real_host = host.rsplit("@", 1)[-1] if "@" in host else host
        # A canonical hostname carries a dot (a TLD or a sub-domain); a dotless
        # token is an SSH config Host ALIAS with no canonical identity, so drop
        # it and keep only the canonical ``owner/repo`` key. This holds whether
        # or not the remote carried a ``user@`` -- a ``user@gh-acct`` alias is no
        # more canonical than a bare ``gh-acct`` one.
        if _is_canonical_host(real_host):
            return f"{real_host}/{path}"
        return path
    return cleaned


def _is_canonical_host(host: str) -> bool:
    """Return True iff ``host`` is a canonical hostname rather than an SSH alias.

    A canonical hostname carries a dot (a registrable domain / sub-domain, e.g.
    ``gitlab.com``, ``github.com``) or is the reserved ``localhost``. A dotless
    token (``gh-acct``, ``work-github``) is an SSH config ``Host`` ALIAS -- a
    local ``~/.ssh/config`` name with no canonical identity -- which must be
    dropped from the slug so the dot-keyed host-strip / visibility probe key on
    the real ``owner/repo`` instead of an unrecognisable alias segment (#1415).
    """
    return "." in host or host == "localhost"


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


def _strip_host_prefix(slug: str) -> str:
    """Drop a leading ``host/`` segment (a first part containing a ``.``).

    A repo identity has two equivalent forms: host-qualified
    (``host/owner/repo``, the origin-remote / ``slug_for_cwd`` / config-doc
    form) and bare (``owner/repo``, the ``gh pr create --repo`` form). The
    host segment is the only difference, and it is recognised the same way the
    visibility probe recognises it -- a first ``/``-segment containing a dot.
    Stripping it yields the form-independent ``owner/repo`` key.
    """
    head, sep, rest = slug.partition("/")
    if sep and "." in head:
        return rest
    return slug


def slug_namespace_matches(entry: str, slug: str) -> bool:
    """Return True iff allowlist ``entry`` matches ``slug`` on path-segment boundaries.

    The canonical key is the host-stripped ``owner/repo`` path. ``entry`` matches
    when, host-stripped, it equals the host-stripped slug OR is a leading run of
    its ``/``-separated segments: ``a`` and ``a/b`` match ``a/b`` and ``a/b/c``,
    but ``a`` does NOT match ``ab/c`` (a substring of a segment) and ``a/b`` does
    NOT match ``a/bc`` (a superset segment). The host segment never participates,
    so an SSH-alias host (``gitlab-<entry>``) or an https host can never satisfy
    the match.

    The match is HOST-QUALIFICATION-SYMMETRIC: both sides are host-stripped first
    (a leading ``/``-segment containing a dot), so a host-qualified entry matches
    a bare ``gh pr create --repo`` slug, a bare entry matches a host-qualified cwd
    slug, and a bare-org entry keeps matching both (#2067).

    This replaces the old case-insensitive SUBSTRING containment, which falsely
    matched an entry appearing anywhere in the slug -- inside an SSH-alias host
    (``gitlab-<entry>:org/public``) or a superset owner (``<entry>-fork/repo``,
    ``open<entry>/repo``) -- and so downgraded a PUBLIC repo to private,
    relaxing the banned-terms gate on a public surface (#1953).
    """
    entry_key = _strip_host_prefix(entry.strip().lower())
    slug_key = _strip_host_prefix(slug.strip().lower())
    if not entry_key or not slug_key:
        return False
    if entry_key == slug_key:
        return True
    entry_parts = entry_key.split("/")
    slug_parts = slug_key.split("/")
    return len(entry_parts) < len(slug_parts) and slug_parts[: len(entry_parts)] == entry_parts


def slug_is_allowlisted_private(slug: str, config_path: Path | None) -> bool:
    """Return True iff ``slug`` matches the offline ``[teatree] private_repos`` allowlist.

    Each entry is matched against the slug's host-stripped ``owner/repo`` path
    segments via :func:`slug_namespace_matches` -- a leading-segment-prefix
    match, NOT a substring. An organisation-namespace entry (``acme-engineering``)
    covers every repo under it (``acme-engineering/secret``, host-qualified or
    bare) while an unrelated superset owner (``acme-engineering-fork``) and an
    SSH-alias host carrying the entry as a substring no longer match.

    The classifier is fail-safe for the leak direction: a True DOWNGRADES the
    banned-terms gate (and makes a publish destination skip the leak scan), so an
    over-match is the dangerous direction. A non-matching, ambiguous, or
    host-root-only entry yields False, which keeps enforcement hard-blocking.
    """
    return any(slug_namespace_matches(entry, slug) for entry in _private_repo_allowlist(config_path))


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
