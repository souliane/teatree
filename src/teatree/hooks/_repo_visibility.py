"""Repo-visibility / privacy resolution for the publish-surface carve-out.

Split out of :mod:`teatree.hooks.publish_surface` to keep that module under
the project's per-file LOC ceiling. This module owns the forge probe and
repo slug resolution; private-entry decisions live in
:mod:`teatree.hooks._private_repo_entries`:

- the cached ``gh``/``glab`` live-visibility probe (best-effort fallback; the
    binary is resolved against an augmented PATH so it works inside the
    restricted PreToolUse subprocess). The cache TTL is asymmetric by risk
    (:func:`_verdict_ttl`): a PUBLIC verdict is held for a day, a non-public one
    for minutes, since only a stale non-public verdict can skip the scan on a
    now-public surface.
- the slug resolution from a repo ``cwd``.

Detection is conservative; an unknown/unresolvable repo is
treated as NOT private so a detection failure never weakens the gate.
"""

import json
import os
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypedDict
from urllib.parse import urlsplit

from teatree.hooks import _private_repo_entries, git_config_offline
from teatree.hooks._forge_tool import FORGE_TOOL, GITHUB, GITLAB, forge_and_repo_path
from teatree.hooks._private_repo_entries import _private_repo_allowlist
from teatree.hooks._ssh_alias import is_canonical_host, ssh_alias_hostname
from teatree.utils.run import CommandFailedError, TimeoutExpired, redact_secrets, run_allowed_to_fail


class _VisibilityEntry(TypedDict):
    """One cached repo-visibility verdict with its capture timestamp."""

    ts: float
    visibility: str


# A slug must have at least ``owner/repo`` (host-prefixed slugs add more).
_MIN_SLUG_PARTS: Final[int] = 2

# How long a cached PUBLIC verdict stays fresh. Repo visibility changes rarely;
# a day-long cache keeps the offline path fast. Staleness in this direction is
# harmless -- a repo that went private is merely scanned as if it were still
# public, which over-scans and never leaks.
_PUBLIC_VERDICT: Final[str] = "PUBLIC"
_PUBLIC_TTL_S: Final[int] = 24 * 60 * 60

# How long a cached NON-PUBLIC verdict (PRIVATE, INTERNAL, ...) stays fresh.
# Far shorter, because this is the only dangerous staleness direction: a
# non-public verdict makes every leak gate SKIP the scan for that slug, so once
# the operator flips the repo public, each minute the stale verdict survives is
# a minute of unscanned public egress.
_NON_PUBLIC_TTL_S: Final[int] = 15 * 60

# Sentinel + TTL for a NEGATIVE cache entry: a probe that RAN but could not
# resolve visibility (tool absent, auth differs, slug unrecognised). Without it,
# every unresolved slug re-probes at the full probe budget PER segment on every
# publish, stacking toward the 30s hook ceiling. The negative entry is short-
# lived (distinct from the 24h PUBLIC TTL) because an unresolvable repo may
# become resolvable soon (auth fixed, tool installed).
_UNKNOWN_VERDICT: Final[str] = "UNKNOWN"
_UNKNOWN_TTL_S: Final[int] = 5 * 60

# Visibility probe budget -- a hook that hangs blocks the user, so the
# network call gets a tight timeout and any failure falls back to "unknown".
# Short first, so a stalled call is cut early; longer second, for a forge that is slow rather than stalled.
_PROBE_ATTEMPT_TIMEOUTS_S: Final[tuple[float, ...]] = (3, 5)
_GIT_REMOTE_TIMEOUT_S: Final[int] = 5
# Worth asking again: a 5xx after `HTTP ` or `glab: `, a stalled or dropped connection, a host short of a resource.
# Anything else, an unprefixed 5xx included, is the forge's answer: the gate refuses, the pre-push listing allows.
_TRANSIENT_FAILURE: Final[re.Pattern[str]] = re.compile(
    r"(?:HTTP |glab: )5\d\d\b"
    r"|(?i:connection reset|temporar(?:ily unavailable|y failure)|i/o timeout|TLS handshake timeout"
    r"|unexpected EOF|context deadline exceeded)"
)
_MAX_CAUSE_CHARS: Final[int] = 400

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

# Why a forge-CLI probe produced no stdout. Kept apart because they have
# DIFFERENT remedies: a timeout says nothing about the credential, so a refusal
# that reports one as the other sends the operator to `auth status` on a CLI
# that is authenticated and would have answered.
PROBE_ABSENT: Final[str] = "tool-absent"
PROBE_UNRUNNABLE: Final[str] = "exec-failed"
PROBE_SPAWN_REFUSED: Final[str] = "a process start the host refused (EAGAIN)"
PROBE_TIMED_OUT: Final[str] = "timeout"
PROBE_NO_TIME: Final[str] = "no time left in the hook budget"


@dataclass(frozen=True, slots=True)
class ForgeProbe:
    """One forge-CLI invocation's stdout, or the CAUSE there is none.

    ``unresolved`` is empty exactly when ``stdout`` is present. Otherwise it is
    what each attempt observed, joined by ``, then ``: a ``PROBE_*`` token, a
    timeout naming its seconds, or the CLI's exit code and stderr — never as
    ``name=value``, which the refusal's credential scrub redacts.
    """

    stdout: str | None
    unresolved: str = ""


def slug_for_cwd(cwd: Path) -> str:
    """Return the ``origin`` slug (``host/owner/repo``) for ``cwd``, or ``""``.

    Resolves ``cwd``'s ``origin`` URL OFFLINE-FIRST (:func:`_origin_remote_url`):
    parsing ``.git/config`` directly needs no ``git`` binary, so the slug resolves
    inside the restricted PreToolUse hook subprocess where a bare ``git`` is
    unresolvable. Normalization is :func:`slug_for_remote_url`. An empty slug fails
    SAFE -- the destination then resolves PUBLIC and the gate stays hard-blocking.
    """
    return slug_for_remote_url(_origin_remote_url(cwd))


def slug_for_remote_url(url: str) -> str:
    """Normalize a remote to ``host/owner/repo`` without network access.

    HTTP(S) and SSH URLs lose scheme, userinfo, port, trailing slash and ``.git``.
    SCP-style paths lose a leading slash. A dotless SSH Host alias is resolved
    through ``ssh -G`` (which reads Include, wildcard Host and Match); an unresolved alias returns ``""`` so visibility
    remains UNKNOWN rather than assigning it the wrong forge host.
    """
    if not url:
        return ""
    cleaned = url.strip().rstrip("/").lower().removesuffix(".git")
    if "://" in cleaned:
        return _url_slug(cleaned)
    if ":" in cleaned and "/" not in cleaned.partition(":")[0]:
        host, _, path = cleaned.partition(":")
        real_host = host.rsplit("@", 1)[-1] if "@" in host else host
        if is_canonical_host(real_host):
            return f"{real_host.lower()}/{path.lstrip('/')}"
        resolved = ssh_alias_hostname(real_host)
        return f"{resolved}/{path.lstrip('/')}" if resolved else ""
    return cleaned


def _url_slug(url: str) -> str:
    """``host/owner/repo`` for a ``scheme://`` remote, userinfo and port dropped.

    A URL with no network host (``file:///srv/repo``) keeps its old verbatim form, so
    a local path is never turned into an ``owner/repo`` some forge would be asked about.
    """
    verbatim = url.split("://", 1)[1]
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return verbatim
    if not host:
        return verbatim
    path = parts.path.strip("/")
    return f"{host}/{path}" if path else host


def _origin_remote_url(cwd: Path) -> str:
    """Resolve ``cwd``'s ``origin`` URL, OFFLINE-FIRST.

    The offline ``.git/config`` parse (:func:`git_config_offline.origin_url`)
    spawns no process, so it resolves the remote inside the restricted
    PreToolUse hook subprocess where a bare ``git`` is unresolvable -- the bug
    that left a flagless ``glab mr create`` / ``gh pr create`` to the user's
    OWN private repo with no slug to match the offline ``private_repos``
    allowlist, over-blocking it. The PATH-augmented subprocess is the fallback
    for the rare configs an offline parse cannot read (e.g. an
    ``[include]``-redirected url).
    """
    offline = git_config_offline.origin_url(cwd)
    if offline:
        return offline
    return _origin_url_via_git(cwd)


def _origin_url_via_git(cwd: Path) -> str:
    """Resolve ``origin`` via ``git remote get-url`` against the augmented PATH.

    The PreToolUse hook subprocess inherits a restricted PATH where a bare
    ``git`` may not resolve; resolving the binary against the augmented probe
    PATH (the same one the visibility probe uses) keeps this fallback working
    in-hook. Any failure (binary absent, not a repo) fails SAFE to ``""``.
    """
    binary = shutil.which("git", path=_probe_search_path())
    if binary is None:
        return ""
    try:
        result = run_allowed_to_fail(
            [binary, "-C", str(cwd), "remote", "get-url", "origin"],
            expected_codes=(0,),
            env=_probe_env(),
            timeout=_GIT_REMOTE_TIMEOUT_S,
        )
    except (CommandFailedError, OSError, TimeoutExpired):
        return ""
    return result.stdout.strip()


def _cache_root() -> Path:
    """Resolve a writable cache dir for the visibility verdict cache.

    HOST-WIDE (:func:`teatree.hooks._hook_state.shared_hook_state_root`), not the per-worktree
    data dir the rest of the hook state uses: the cached fact belongs to the remote
    repo, so every checkout on this host must read the same answer. If the chosen
    root already exists as a non-directory, fall back to a sibling so the write
    still succeeds.
    """
    from teatree.hooks._hook_state import shared_hook_state_root  # noqa: PLC0415 — deferred: cold-hook top

    root = shared_hook_state_root()
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
    if time.time() - ts > _verdict_ttl(verdict):
        return None
    return verdict


def _verdict_ttl(verdict: str) -> int:
    """How long a cached ``verdict`` stays fresh, per its staleness risk."""
    if verdict == _UNKNOWN_VERDICT:
        return _UNKNOWN_TTL_S
    return _PUBLIC_TTL_S if verdict == _PUBLIC_VERDICT else _NON_PUBLIC_TTL_S


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


def run_forge_tool(tool: str, args: list[str], *, budget: Callable[[float], float | None] | None = None) -> ForgeProbe:
    """Run ``tool`` with *args* against the augmented probe PATH.

    An absent :attr:`ForgeProbe.stdout` means the question went unasked, and
    :attr:`ForgeProbe.unresolved` says WHICH way — the outcomes are kept apart
    because a caller that REFUSES on the answer has to name the cause it
    observed instead of asserting one. Only a transient is asked again — a
    timeout, a 5xx, a stalled or dropped connection, a process start the host
    refused (EAGAIN): any other outcome is the forge's answer, and repeating it
    would only repeat it. *budget* maps an attempt's timeout to what the caller
    can still afford, ``None`` for nothing, so a hook starts no attempt its
    ceiling would cancel. Shared with the foreign-open-MR guard
    (:mod:`teatree.hooks.foreign_mr_cli`) so both forge probes resolve their
    binary and their environment identically.
    """
    binary = _resolve_probe_tool(tool)
    if binary is None:
        return ForgeProbe(stdout=None, unresolved=PROBE_ABSENT)
    observed: list[str] = []
    for preferred in _PROBE_ATTEMPT_TIMEOUTS_S:
        timeout = preferred if budget is None else budget(preferred)
        if timeout is None:
            observed.append(PROBE_NO_TIME)
            break
        try:
            result = run_allowed_to_fail([binary, *args], expected_codes=(0,), env=_probe_env(), timeout=timeout)
        except TimeoutExpired:
            observed.append(f"{PROBE_TIMED_OUT} of {timeout:.3g}s")
        except CommandFailedError as error:
            observed.append(_exit_cause(error))
            if not _TRANSIENT_FAILURE.search(error.stderr):
                break
        except BlockingIOError:
            observed.append(PROBE_SPAWN_REFUSED)
        except OSError:
            observed.append(PROBE_UNRUNNABLE)
            break
        else:
            return ForgeProbe(stdout=result.stdout)
    return ForgeProbe(stdout=None, unresolved=", then ".join(observed))


def _exit_cause(error: CommandFailedError) -> str:
    stderr = redact_secrets("; ".join(line.strip() for line in error.stderr.splitlines() if line.strip()))
    return (
        f"exit {error.returncode}: {stderr[:_MAX_CAUSE_CHARS]}" if stderr else f"exit {error.returncode} with no stderr"
    )


def _probe_gh(repo_path: str) -> str | None:
    stdout = run_forge_tool(
        FORGE_TOOL[GITHUB],
        ["repo", "view", repo_path, "--json", "visibility", "--jq", ".visibility"],
    ).stdout
    if stdout is None:
        return None
    verdict = stdout.strip().upper()
    return verdict or None


def _probe_glab(repo_path: str) -> str | None:
    # ``glab api`` has no ``--jq`` flag (unlike ``gh``), so the verdict is
    # parsed from the full project JSON in Python. Passing ``--jq`` makes glab
    # exit non-zero with "Unknown flag", silently defeating the carve-out for
    # every GitLab repo.
    stdout = run_forge_tool(FORGE_TOOL[GITLAB], ["api", f"projects/{repo_path.replace('/', '%2F')}"]).stdout
    if stdout is None:
        return None
    try:
        project = json.loads(stdout)
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

    The forge is resolved from the slug's host segment by the shared
    :func:`_forge_tool.forge_and_repo_path`, so this probe and the foreign-MR
    guard route identically. A BARE ``owner/repo`` slug carries no host, so
    callers that know the forge from the publish tool qualify the slug UP to its
    canonical host form first (:func:`forge_qualified_slug`) -- a ``glab`` post
    therefore probes via ``glab`` instead of being mis-routed to the GitHub
    default.
    """
    forge, repo_path = forge_and_repo_path(slug)
    if forge == GITLAB:
        return _probe_glab(repo_path)
    if forge == GITHUB:
        return _probe_gh(repo_path)
    return None


# Canonical host for each forge, used to qualify a BARE ``owner/repo`` slug UP
# to its host-prefixed form so the host-keyed probe routes to the right tool.
_FORGE_CANONICAL_HOST: Final[dict[str, str]] = {GITHUB: "github.com", GITLAB: "gitlab.com"}


def forge_qualified_slug(slug: str, forge: str) -> str:
    """Return ``slug`` host-qualified with ``forge``'s canonical host, if it is bare.

    A bare ``owner/repo`` slug (no leading dotted host segment) carries no forge
    of its own, so the host-keyed :func:`probe_visibility` defaults it to the
    GitHub probe -- which can never confirm a GitLab repo private. When the
    publish TOOL pins the forge (``glab`` -> ``gitlab``, ``gh`` -> ``github``),
    qualifying the slug UP to ``<host>/owner/repo`` routes the probe to the right
    tool. An already host-qualified slug, an unknown forge, or an empty slug is
    returned unchanged -- the host segment then governs the route, and the
    allowlist entries are host-qualified, so qualification is required before matching.
    """
    canonical_host = _FORGE_CANONICAL_HOST.get(forge)
    if not canonical_host or not slug:
        return slug
    head = slug.split("/", 1)[0]
    if "." in head:
        return slug
    return f"{canonical_host}/{slug}"


def slug_visibility(slug: str) -> str | None:
    """Resolve ``slug``'s visibility verdict (upper-cased), or ``None`` (cache -> probe -> cache).

    ``None`` is the fail-safe unknown -- an absent probe tool, an unrecognised
    slug, or a probe error. Callers read a ``"PRIVATE"`` verdict (the carve-out)
    or a ``"PUBLIC"`` one (the affirmative-public leak-gate scope in
    :mod:`teatree.hooks.public_visibility`); every other verdict, and ``None``,
    is neither.

    A negative (``None``) probe result is short-TTL cached under the
    :data:`_UNKNOWN_VERDICT` sentinel so an unresolvable slug is not re-probed at
    the full probe budget on every publish -- the read maps that sentinel back to
    ``None`` for callers.
    """
    cached = _read_visibility_cache(slug)
    if cached is not None:
        return None if cached == _UNKNOWN_VERDICT else cached
    verdict = probe_visibility(slug)
    _write_visibility_cache(slug, verdict if verdict is not None else _UNKNOWN_VERDICT)
    return verdict


def slug_is_private(slug: str) -> bool:
    """Return True iff ``slug``'s visibility verdict is ``"PRIVATE"``."""
    return slug_visibility(slug) == "PRIVATE"


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


def slug_segment_depth(entry: str) -> int:
    """How many host-stripped path segments *entry* pins — a pattern's SPECIFICITY.

    ``org`` is 1 and ``org/sub/repo`` is 3, host-qualified or not, so two patterns
    written in different forms compare on the same axis
    :func:`slug_namespace_matches` matches on. A blank or host-root-only entry is 0,
    below every real pattern.
    """
    key = _strip_host_prefix(entry.strip().lower())
    return len([segment for segment in key.split("/") if segment])


def slug_is_allowlisted_private(slug: str, config_path: Path | None) -> bool:
    """Match only valid host-qualified entries against a remote on segment boundaries."""
    return any(
        _private_repo_entries.private_repo_entry_matches(entry, slug, normalize=slug_for_remote_url)
        for entry in _private_repo_allowlist(config_path)
    )


def term_is_own_repo_slug(term: str, config_path: Path | None = None) -> bool:
    """Return True iff ``term`` is (a token-run of) a ``private_repos`` entry.

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
    from teatree.hooks.term_match import _contains_run, tokens  # noqa: PLC0415 — deferred: call-time import, kept lazy

    term_tokens = tokens(term)
    if not term_tokens:
        return False
    return any(
        _contains_run(tokens(entry.partition("/")[2]), term_tokens) for entry in _private_repo_allowlist(config_path)
    )
