"""Destination-aware gate skip for the pre-publish gates (publish-surface purpose).

The banned-terms (#1415) and bare-reference (#1530) gates exist to stop
leaks on PUBLIC surfaces. Firing on EVERY publish command -- including
writes to an INTERNAL/PRIVATE repo or namespace -- over-blocks: a private
repo's own customer/domain terms and bare cross-references are exactly
what its issues/PRs are supposed to carry.

:func:`resolve_publish_destination` extracts the target repo/namespace
from the COMMAND ITSELF (the ``--repo``/``-R`` flag, the ``api`` URL path,
or the cwd git remote) and :func:`is_public_destination` classifies THAT
resolved target FAIL-CLOSED against an INTERNAL DENYLIST: a destination is
PUBLIC (gate scans/blocks) UNLESS it is PROVABLY internal -- its namespace
matches the config-driven ``[teatree] internal_publish_namespaces`` allowlist
(or the ``T3_INTERNAL_PUBLISH_NAMESPACES`` env var), the
``[teatree] private_repos`` allowlist, or the day-cached ``gh``/``glab``
live-visibility probe returns a CONFIRMED-PRIVATE verdict. Every OTHER target
-- a genuinely-public non-teatree repo (a user's other public repos), a
third-party repo, an UNKNOWN-visibility target, or an UNRESOLVABLE target --
stays PUBLIC and is SCANNED. This is the only safe default: an allowlist of
"surfaces to scan" would fail OPEN on a public repo nobody remembered to list,
leaking an internal term unscanned onto a public surface. Resolving the target
from the command rather than the harness cwd is what lets a post FROM a public
clone TO a provably-private repo skip the public-leak scan instead of
over-blocking. With nothing configured and no probe-resolvable private verdict,
every destination stays PUBLIC, so behaviour is conservative for unconfigured
users. :func:`gate_skips_destination` is the composed predicate the gates call.

The hook process is overlay-agnostic and cannot import ``OverlayConfig``; it
reads the internal denylist from ``~/.teatree.toml`` DIRECTLY (the
``internal_publish_namespaces`` / ``private_repos`` readers in
:mod:`teatree.hooks._repo_visibility` and this module). The canonical public
teatree repo needs no entry -- it is public, so the fail-closed default already
scans it.

The shared command-parsing helpers (``_extract_repo_flag``, the
eligible-verb sets) live in :mod:`teatree.hooks.publish_surface` and the
repo-target resolution (``slug_for_cwd``, ``_config_path``) in
:mod:`teatree.hooks._repo_visibility`; this module reuses them so the
repo-target resolution stays in one place across both the private-repo
carve-out and the destination skip.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from teatree.hooks._command_parser import first_segment_words
from teatree.hooks._gh_glab_hiding import command_segments, token_has_substitution_marker, token_is_transport_construct
from teatree.hooks._publish_detection import segment_is_api_read as _segment_is_api_read
from teatree.hooks._publish_detection import segment_is_api_write as _segment_is_api_write
from teatree.hooks._repo_visibility import (
    _config_path,
    forge_qualified_slug,
    slug_for_cwd,
    slug_is_allowlisted_private,
    slug_is_private,
    slug_namespace_matches,
)
from teatree.hooks.publish_surface import (
    _GH_ELIGIBLE_VERBS,
    _GLAB_ELIGIBLE_VERBS,
    _extract_repo_flag,
    _segment_is_publish_inert,
    _strip_benign_prefix,
)


@dataclass(frozen=True)
class Destination:
    """The target a publish command writes to.

    ``slug`` is the repo/namespace as it appears on the command line
    (``owner/repo``, ``host/owner/repo``, or ``ns/sub/repo``). ``via``
    records how it was resolved (``flag`` for ``--repo``/``-R``, ``api`` for
    a ``gh``/``glab api`` URL path, ``url`` for a forge URL positional, ``env``
    for ``GH_REPO``, ``cwd`` for the current-repo fallback) so a caller can log
    the provenance without re-parsing.

    ``forge`` records which forge the PUBLISH TOOL targets (``github`` for a
    ``gh`` command, ``gitlab`` for a ``glab`` command, ``""`` when unknown). A
    bare ``owner/repo`` slug carries no host segment, so the visibility probe
    cannot tell GitHub from GitLab from the slug alone and defaulted every
    flagless target to the GitHub probe -- an internal/private GitLab MR was
    then probed via ``gh``, never confirmed private, and the gate over-fired.
    The tool word is known at resolution time, so ``forge`` carries it to
    :func:`is_public_destination`, which forwards it to the probe (see
    :func:`_repo_visibility.slug_is_private`). A host-qualified slug already
    pins the forge from its host segment, so the hint only matters for the
    bare-slug case.
    """

    slug: str
    via: str
    forge: str = ""


# ``gh api [/]repos/<owner>/<repo>/...`` -- the slug is the path segment after
# ``repos/``. The trailing path (``/issues``, ``/pulls/1/comments``) is
# discarded; only ``owner/repo`` identifies the destination. ``gh`` accepts
# the endpoint with or without a leading ``/``.
_GH_API_REPOS_RE: Final[re.Pattern[str]] = re.compile(r"^/?repos/([^/]+/[^/]+)")

# ``glab api [/]projects/<url-encoded ns%2Frepo>/...`` -- the project is a
# single URL-encoded path segment (``ns%2Frepo`` or ``ns%2Fsub%2Frepo``).
# ``%2F`` decodes back to ``/`` so the slug matches the allowlist shape.
_GLAB_API_PROJECTS_RE: Final[re.Pattern[str]] = re.compile(r"^/?projects/([^/?]+)")

# The ``owner/repo`` of a forge URL positional, before the resource segment
# (GitLab ``/-/`` infix and nested group paths handled; ``.git`` suffix stripped).
_FORGE_URL_SLUG_RE: Final[re.Pattern[str]] = re.compile(
    r"https?://(?:[\w.-]+\.)?(?:github\.com|gitlab\.com)/"
    r"(?P<slug>[\w.-]+(?:/[\w.-]+)+?)"
    r"(?:/(?:-/)?(?:issues|pull|pulls|merge_requests|commit|tree|blob)\b|\.git\b|/?$)",
)

# ``gh``/``glab`` create/comment verbs whose target, when no ``--repo``/``-R``
# flag is present, is the CURRENT repo (resolved from the git remote).
_CURRENT_REPO_VERBS: Final[frozenset[tuple[str, str]]] = _GH_ELIGIBLE_VERBS | _GLAB_ELIGIBLE_VERBS

# Flags whose VALUE is the next token in a ``gh``/``glab api`` call -- skipped
# (flag + value) so the bare endpoint URL is found regardless of ordering.
# A flag NOT in this set and NOT in ``_API_BOOLEAN_FLAGS`` makes the URL
# resolution AMBIGUOUS (its value could be misread as the endpoint, or the
# endpoint as its value), so ``_api_url_arg`` fails closed on it.
_API_VALUE_FLAGS: Final[frozenset[str]] = frozenset(
    {
        "-f",
        "-F",
        "--field",
        "--raw-field",
        "-X",
        "--method",
        "--input",
        "-H",
        "--header",
        "--hostname",
        "-q",
        "--jq",
        "-t",
        "--template",
        "--cache",
        "-p",
        "--preview",
    }
)

# ``gh``/``glab api`` flags that take NO value -- skipped (flag only) so a
# ``--paginate`` before the endpoint does not derail URL resolution. CLOSED
# enumeration: an unrecognised flag is ambiguous and fails resolution closed.
_API_BOOLEAN_FLAGS: Final[frozenset[str]] = frozenset(
    {"--paginate", "-i", "--include", "--silent", "--verbose", "--slurp"}
)

# Leading executables a no-destination chained segment may carry and still be
# provably skip-safe: navigation / local-only / git-transport commands that
# cannot themselves post a body to a forge issue/PR/MR surface. This is a
# CLOSED POSITIVE allowlist, not a denylist of "publishing" tools: an
# UNRECOGNISED leader (``make``, ``npm``, ``python``, ``./release.sh``, an
# interpreter, an ``ssh``/``xargs`` wrapper) can shell out to ``gh``/``curl``
# with no literal forge token in its own argv, so it is NOT provably inert and
# the whole command fails closed (scans). ``git`` is included because a
# ``git push`` carries commits to a git remote -- the COMMIT gate's surface,
# not a forge body the destination skip governs -- mirroring the commit
# chain's treatment of ``git push`` as publish-inert.
_SKIP_INERT_LEADERS: Final[frozenset[str]] = frozenset({"cd", "pushd", "popd", "echo", "printf", "true", ":", "git"})


def _forge_for_tool(tool: str) -> str:
    """Map a publish tool word to its forge: ``gh`` -> github, ``glab`` -> gitlab.

    The forge is what lets the visibility probe pick ``gh`` vs ``glab`` for a
    BARE ``owner/repo`` slug that carries no host segment of its own. An
    unrecognised leader yields ``""`` (no hint; the probe falls back to the
    slug's own host or the GitHub default).
    """
    if tool == "gh":
        return "github"
    if tool == "glab":
        return "gitlab"
    return ""


def _api_url_arg(words: list[str]) -> str | None:
    """Return the first non-flag positional after ``gh``/``glab`` ``api``, or ``None``.

    The URL path is the first WORD token following the ``api`` sub-command
    that is not itself a flag or a flag value. Known value-taking flags are
    skipped with their value, known boolean flags and self-contained
    ``--flag=value`` tokens alone, so the bare endpoint path is found
    regardless of flag ordering. An UNRECOGNISED separated flag makes the
    resolution AMBIGUOUS -- whether the next token is its value or the
    endpoint is unknowable without that flag's arity, and a wrong guess
    would let a flag VALUE that merely looks like an internal repo path
    stand in for the real endpoint -- so resolution fails closed (``None``,
    which the callers classify PUBLIC/unprovable).
    """
    try:
        start = words.index("api") + 1
    except ValueError:
        return None
    i = start
    while i < len(words):
        w = words[i]
        if w in _API_VALUE_FLAGS:
            i += 2
            continue
        if w.startswith("-") and w != "-":
            if w in _API_BOOLEAN_FLAGS or (w.startswith("--") and "=" in w):
                i += 1
                continue
            return None
        return w
    return None


def _destination_from_api(words: list[str], tool: str) -> Destination | None:
    """Resolve the destination of a ``gh api`` / ``glab api`` raw REST call."""
    url = _api_url_arg(words)
    if url is None:
        return None
    forge = _forge_for_tool(tool)
    if tool == "gh":
        match = _GH_API_REPOS_RE.match(url)
        return Destination(slug=match.group(1), via="api", forge=forge) if match else None
    match = _GLAB_API_PROJECTS_RE.match(url)
    if match:
        return Destination(slug=match.group(1).replace("%2F", "/").replace("%2f", "/"), via="api", forge=forge)
    return None


def _destination_from_current_repo(cwd: Path | None, forge: str) -> Destination | None:
    """Resolve a flagless create/comment command's target from the git remote."""
    if cwd is None:
        return None
    slug = slug_for_cwd(cwd)
    return Destination(slug=slug, via="cwd", forge=forge) if slug else None


def _destination_from_forge_url(words: list[str], forge: str) -> Destination | None:
    """Resolve the target from the FIRST forge URL positional in ``words``, or ``None``.

    ``gh issue comment https://github.com/owner/repo/issues/5`` names its target
    by URL with no ``--repo`` flag; the slug is the path before the resource
    segment. This is more specific than the cwd remote, so it is resolved before
    the current-repo fallback.
    """
    for word in words:
        match = _FORGE_URL_SLUG_RE.search(word)
        if match:
            return Destination(slug=match.group("slug").removesuffix(".git"), via="url", forge=forge)
    return None


def _flagless_destination(words: list[str], tool: str, cwd: Path | None) -> Destination | None:
    """Resolve a publish target when no explicit ``--repo``/``-R`` flag is present.

    Priority: a raw-REST ``api`` URL path, then the ``gh`` ``GH_REPO`` env
    default, then a forge URL positional in the args, then the current repo for a
    create/comment/note verb. ``None`` when none of these resolves a target.
    """
    forge = _forge_for_tool(tool)
    if "api" in words:
        return _destination_from_api(words, tool)
    if tool == "gh" and os.environ.get("GH_REPO", "").strip():
        return Destination(slug=os.environ["GH_REPO"].strip(), via="env", forge=forge)
    url_dest = _destination_from_forge_url(words, forge)
    if url_dest is not None:
        return url_dest
    if len(words) >= 3 and (words[1], words[2]) in _CURRENT_REPO_VERBS:  # noqa: PLR2004
        return _destination_from_current_repo(cwd, forge)
    return None


def _destination_from_words(words: list[str], cwd: Path | None) -> Destination | None:
    """Resolve the publish destination of one command segment's word list.

    The visibility-independent half of :func:`resolve_publish_destination`,
    factored out so :func:`gate_skips_destination` can resolve a destination
    PER top-level segment (the ALL-SEGMENTS invariant) rather than only from
    the first segment.
    """
    if not words or words[0] not in {"gh", "glab"}:
        return None
    explicit = _extract_repo_flag(words)
    if explicit:
        return Destination(slug=explicit, via="flag", forge=_forge_for_tool(words[0]))
    return _flagless_destination(words, words[0], cwd)


def resolve_publish_destination(command: str, cwd: Path | None = None) -> Destination | None:
    """Extract the target repo/namespace of a publish ``command``, or ``None``.

    Covers the structured and raw-REST publish surfaces:

    - ``glab ... -R <ns>/<repo>`` / ``gh ... -R <owner>/<repo>`` -- the
        explicit ``--repo``/``-R`` flag (LAST-WINS, mirroring gh/glab).
    - ``gh api repos/<owner>/<repo>/...`` -- the ``repos/`` path segment.
    - ``glab api projects/<url-encoded ns%2Frepo>/...`` -- the ``projects/``
        path segment, ``%2F``-decoded.
    - ``gh``/``glab`` ``pr``/``issue``/``mr`` ``create``/``comment``/``note``
        with no ``--repo`` flag -- the CURRENT repo, via the git remote of
        ``cwd``.

    Resolves only the FIRST command segment; :func:`gate_skips_destination`
    is the multi-segment predicate. Returns ``None`` when the target cannot
    be determined (a non-publish command, a ``curl``/Slack surface, a
    flagless API call, or a flagless create with no resolvable git remote).
    ``None`` is the caller's signal to treat the destination as PUBLIC and
    scan (fail-closed).
    """
    return _destination_from_words(first_segment_words(command), cwd)


def _segment_carries_substitution_or_transport(words: list[str]) -> bool:
    """Return True iff any token is a substitution marker or transport construct.

    A ``$(...)`` / backtick / process-substitution token, or a
    redirection/here-doc/group-opener token, can run a SECOND command (a
    public post) when the shell expands the line -- so the gate must NOT skip
    and must scan instead. Mirrors the carve-out's fail-closed posture on
    these constructs (a quoted flag value carrying ``$(...)`` still trips the
    substitution check, since a public post can hide inside a body value).
    """
    return any(token_has_substitution_marker(token) or token_is_transport_construct(token) for token in words)


def _segment_is_skip_inert(words: list[str]) -> bool:
    """Return True iff a no-destination segment is PROVABLY safe to skip scanning.

    A chained segment that resolves to no publish destination is skip-safe ONLY
    when it is a recognised navigation / local-only / git-transport command --
    its leading executable (after a benign ``cd <path>`` / ``VAR=value`` prefix)
    is in the CLOSED :data:`_SKIP_INERT_LEADERS` allowlist -- AND it carries no
    forge token or substitution/transport construct
    (:func:`publish_surface._segment_is_publish_inert`).

    The leader allowlist is the closed-enumeration half the destination skip
    needs that ``_segment_is_publish_inert`` alone does not give: that predicate
    only proves the ABSENCE of a literal ``gh``/``glab``/``curl`` token, so an
    unrecognised executable (``make publish``, ``npm run release``,
    ``python deploy.py``, ``./release.sh``) -- which can shell out to a public
    post with no forge token in its own argv -- would otherwise pass it and let
    a leading internal segment skip the whole command's leak scan. Requiring a
    recognised inert leader fails closed on every such wrapper, mirroring the
    prove-pure-or-fail-closed inversion rather than enumerating the wrappers.
    """
    rest = _strip_benign_prefix(words)
    if not rest:
        return True
    return rest[0] in _SKIP_INERT_LEADERS and _segment_is_publish_inert(words)


def _teatree_list_setting(key: str, env_var: str, config_path: Path | None) -> list[str]:
    """Return a ``[teatree] <key>`` list unioned with ``<env_var>`` (lower-cased).

    The env var (comma- or space-separated) supplements the TOML list, mirroring
    the established ``internal_publish_namespaces`` / ``T3_INTERNAL_PUBLISH_NAMESPACES``
    shape. Reads the TOML directly (no Django/config import) to stay importable
    from the hook process.
    """
    env_raw = os.environ.get(env_var, "")
    env_entries = [e.strip().lower() for e in re.split(r"[,\s]+", env_raw) if e.strip()]

    import tomllib  # noqa: PLC0415

    target = config_path if config_path is not None else _config_path()
    toml_entries: list[str] = []
    if target.is_file():
        try:
            raw = tomllib.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        teatree = raw.get("teatree", {}) if isinstance(raw, dict) else {}
        entries = teatree.get(key, []) if isinstance(teatree, dict) else []
        if isinstance(entries, list):
            toml_entries = [str(e).strip().lower() for e in entries if str(e).strip()]
    return env_entries + toml_entries


def _internal_publish_namespaces(config_path: Path | None = None) -> list[str]:
    """Return the ``[teatree] internal_publish_namespaces`` denylist (lower-cased).

    The list of host/namespace prefixes that are PROVABLY internal. Read
    from the ``T3_INTERNAL_PUBLISH_NAMESPACES`` env var first (comma- or
    space-separated, for a quick per-session override), then the
    ``[teatree] internal_publish_namespaces`` key in ``~/.teatree.toml``.
    DEFAULT is empty -- with nothing configured every destination stays PUBLIC
    (scanned), so behaviour is conservative for unconfigured users.

    No real company/customer namespace is hardcoded here; the denylist lives
    only in the user's private config / env.
    """
    return _teatree_list_setting("internal_publish_namespaces", "T3_INTERNAL_PUBLISH_NAMESPACES", config_path)


def is_public_destination(dest: Destination | None, *, config_path: Path | None = None) -> bool:
    """Return True iff ``dest`` should be treated as a PUBLIC publish target.

    FAIL-CLOSED classification: a destination is PUBLIC (the gate scans and
    blocks) UNLESS it is PROVABLY internal. A destination is internal when ANY
    of these resolves its slug to private:

    - the ``[teatree] internal_publish_namespaces`` /
        ``T3_INTERNAL_PUBLISH_NAMESPACES`` denylist, as a case-insensitive
        prefix-SEGMENT match (``internalcorp`` matches ``internalcorp/svc``
        and ``host/internalcorp/svc`` but not ``other/internalcorp-public``);
    - the existing ``[teatree] private_repos`` allowlist that the
        commit / pure-post carve-out already consults
        (:func:`_repo_visibility.slug_is_allowlisted_private`), so a user's
        CURRENT ``private_repos`` config makes their private namespaces skip the
        public-leak scan without maintaining a second list;
    - the day-cached ``gh``/``glab`` live-visibility probe
        (:func:`_repo_visibility.slug_is_private`) returning a CONFIRMED-PRIVATE
        verdict. Resolving visibility from the COMMAND's target slug (the
        ``--repo``/``-R`` flag, the ``api`` URL path, or the cwd remote) rather
        than the harness cwd is what lets a post FROM a public clone TO a
        provably-private repo skip the public-leak scan instead of over-blocking.
        The publish tool's forge (``dest.forge`` -- ``github`` for ``gh``,
        ``gitlab`` for ``glab``) is forwarded to the probe so a BARE GitLab slug
        is probed via ``glab`` rather than mis-routed to the GitHub default; the
        probe returns ``None`` (unknown -- tool absent in-hook or auth differs)
        for an unresolvable target, which stays PUBLIC.

    Every OTHER target stays PUBLIC and is SCANNED: a genuinely-public
    non-teatree repo (a user's other public repos), a third-party repo, an
    UNKNOWN-visibility target, a ``None`` destination (unresolvable target), an
    empty slug, and a slug carrying an unexpanded shell variable (``$``, runtime
    value unknowable). The public-surface default is fail-closed because an
    allowlist of "surfaces to scan" would fail OPEN on a public repo nobody
    remembered to list, leaking an internal term unscanned onto a public surface.
    A probe that cannot prove the target private leaves it PUBLIC -- detection
    failure never weakens the gate.
    """
    if dest is None:
        return True
    slug = dest.slug.strip().lower()
    if not slug or "$" in slug:
        return True
    if any(slug_namespace_matches(entry, slug) for entry in _internal_publish_namespaces(config_path)):
        return False
    if slug_is_allowlisted_private(slug, config_path):
        return False
    # Qualify a BARE slug UP to its forge's canonical host so the host-keyed
    # probe routes to the right tool: a ``glab`` post probes via ``glab``, not
    # the GitHub default. A host-qualified slug is unchanged.
    probe_slug = forge_qualified_slug(slug, dest.forge)
    return not slug_is_private(probe_slug)


def _api_write_targets_internal_repo(words: list[str], *, config_path: Path | None = None) -> bool:
    """Return True iff a raw ``api`` WRITE segment provably targets an internal repo.

    A ``gh api`` / ``glab api`` write carries its body only to the endpoint its
    URL path names. When that path resolves to a repo slug
    (``repos/<owner>/<repo>`` / ``projects/<ns>%2F<repo>``) that is provably
    internal, the write cannot leak to a public surface -- updating an MR
    description on a private customer project is the canonical case. The slug
    must come from the URL path itself (``via="api"``): an ``-R`` flag does not
    constrain a raw endpoint. An unresolvable path (a shell variable, a
    flagless call, an ambiguous unknown flag, a non-repo endpoint) or a
    public/unknown-visibility slug stays fail-closed.
    """
    if not words or words[0] not in {"gh", "glab"}:
        return False
    dest = _destination_from_api(words, words[0])
    if dest is None or dest.via != "api":
        return False
    return not is_public_destination(dest, config_path=config_path)


def gate_skips_destination(command: str, cwd: Path | None, *, config_path: Path | None = None) -> bool:
    """Return True iff a publish-surface gate should SKIP scanning ``command``.

    The banned-terms / bare-reference gates scan only PUBLIC targets. The
    skip is the ALL-SEGMENTS inversion that mirrors
    :func:`publish_surface.command_is_pure_private_gh_glab_post`: skip ONLY
    when EVERY top-level segment is provably safe to skip and there is at
    least one publish segment. A single public, unresolvable, ``api`` WRITE, or
    substitution/transport-carrying segment makes the WHOLE command scan
    (fail-closed). Otherwise a chained or substituted public post hides
    behind a leading internal segment and is never scanned.

    A segment is skip-safe when it is one of:

    - a publish segment whose destination resolves to a provably-INTERNAL
        repo/namespace, which carries no substitution/transport construct;
    - a raw ``gh``/``glab api`` WRITE whose URL path itself resolves to a
        provably-INTERNAL repo (:func:`_api_write_targets_internal_repo`) --
        the body lands only on that private project's surface, so updating
        e.g. a private customer MR description is not a public leak. An api
        WRITE with an unresolvable path (shell variable, non-repo endpoint)
        or a public/unknown target still fails closed;
    - a read-only ``gh``/``glab api`` GET (:func:`_segment_is_api_read`) --
        a read posts NO body, so it can never leak content regardless of the
        repo its URL names, and is skip-safe without resolving a destination; or
    - a segment that is PROVABLY a recognised navigation / local-only /
        git-transport command (:func:`_segment_is_skip_inert` -- its leading
        executable is in the closed ``_SKIP_INERT_LEADERS`` allowlist, e.g.
        ``cd``/``echo``/``git push``, with no forge token or
        substitution/transport construct).

    Every OTHER segment is NOT skip-safe and makes the whole command scan
    (fail-closed): a raw ``gh api`` / ``glab api`` WRITE whose URL does not
    prove an internal repo target (it carries a body to an arbitrary
    endpoint), a
    ``$(...)`` / process-substitution / redirection construct, a PUBLIC or
    unresolvable publish destination, and -- the closed inversion -- ANY
    segment whose leading word is an UNRECOGNISED executable (an interpreter
    ``sh``/``bash``/``eval``, an ``ssh``/``xargs`` wrapper, a build/script
    runner ``make``/``npm``/``python``/``./release.sh``, ...). Such a segment
    resolves to no destination and is not a recognised inert leader, so it
    could shell out to a hidden public post with no forge token in its own
    argv; skipping on the strength of a sibling internal segment is exactly the
    leak this guards. This mirrors the commit chain's prove-pure-or-fail-closed
    inversion rather than enumerating transports.
    """
    segments = command_segments(command)
    if not segments:
        return False
    saw_internal_publish = False
    for words in segments:
        if _segment_carries_substitution_or_transport(words):
            return False
        if _segment_is_api_write(words):
            if not _api_write_targets_internal_repo(words, config_path=config_path):
                return False
            saw_internal_publish = True
            continue
        if _segment_is_api_read(words):
            continue
        dest = _destination_from_words(words, cwd)
        if dest is not None:
            if is_public_destination(dest, config_path=config_path):
                return False
            saw_internal_publish = True
        elif not _segment_is_skip_inert(words):
            return False
    return saw_internal_publish
