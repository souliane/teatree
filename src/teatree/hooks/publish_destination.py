"""Destination-aware gate skip for the pre-publish gates (publish-surface purpose).

The banned-terms (#1415) and bare-reference (#1530) gates exist to stop
leaks on PUBLIC surfaces. Firing on EVERY publish command -- including
writes to an INTERNAL/PRIVATE repo or namespace -- over-blocks: a private
repo's own customer/domain terms and bare cross-references are exactly
what its issues/PRs are supposed to carry.

:func:`resolve_publish_destination` extracts the target repo/namespace
from the COMMAND ITSELF (the ``--repo``/``-R`` flag, the ``api`` URL path,
or the cwd git remote) and :func:`is_public_destination` classifies THAT
resolved target FAIL-CLOSED: a destination is PUBLIC (gate scans/blocks)
UNLESS it is PROVABLY internal -- its namespace matches the CONFIG-DRIVEN
``[teatree] internal_publish_namespaces`` allowlist (or the
``T3_INTERNAL_PUBLISH_NAMESPACES`` env var), the ``[teatree] private_repos``
allowlist, or the day-cached ``gh``/``glab`` live-visibility probe (the same
fallback the private-repo carve-out applies to its command-resolved target).
Resolving visibility from the command's target rather than the harness cwd
is what lets a post FROM a public clone TO a provably-private repo skip the
public-leak scan instead of over-blocking. With no allowlist configured and
no probe-resolvable target, every destination stays PUBLIC, so behaviour is
UNCHANGED for unconfigured users. An unresolvable destination is PUBLIC
(scan). :func:`gate_skips_destination` is the composed predicate the gates
call.

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
from teatree.hooks._publish_detection import segment_is_api_call as _segment_is_api_call
from teatree.hooks._repo_visibility import _config_path, slug_for_cwd, slug_is_allowlisted_private, slug_is_private
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
    """

    slug: str
    via: str


# ``gh api repos/<owner>/<repo>/...`` -- the slug is the path segment after
# ``repos/``. The trailing path (``/issues``, ``/pulls/1/comments``) is
# discarded; only ``owner/repo`` identifies the destination.
_GH_API_REPOS_RE: Final[re.Pattern[str]] = re.compile(r"^repos/([^/]+/[^/]+)")

# ``glab api projects/<url-encoded ns%2Frepo>/...`` -- the project is a
# single URL-encoded path segment (``ns%2Frepo`` or ``ns%2Fsub%2Frepo``).
# ``%2F`` decodes back to ``/`` so the slug matches the allowlist shape.
_GLAB_API_PROJECTS_RE: Final[re.Pattern[str]] = re.compile(r"^projects/([^/?]+)")

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
_API_VALUE_FLAGS: Final[frozenset[str]] = frozenset(
    {"-f", "-F", "--field", "--raw-field", "-X", "--method", "--input", "-H", "--header"}
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


def _api_url_arg(words: list[str]) -> str | None:
    """Return the first non-flag positional after ``gh``/``glab`` ``api``.

    The URL path is the first WORD token following the ``api`` sub-command
    that is not itself a flag or a flag value. Value-taking field flags and
    their values are skipped so the bare endpoint path is found regardless
    of flag ordering.
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
        if w.startswith("-"):
            i += 1
            continue
        return w
    return None


def _destination_from_api(words: list[str], tool: str) -> Destination | None:
    """Resolve the destination of a ``gh api`` / ``glab api`` raw REST call."""
    url = _api_url_arg(words)
    if url is None:
        return None
    if tool == "gh":
        match = _GH_API_REPOS_RE.match(url)
        return Destination(slug=match.group(1), via="api") if match else None
    match = _GLAB_API_PROJECTS_RE.match(url)
    if match:
        return Destination(slug=match.group(1).replace("%2F", "/").replace("%2f", "/"), via="api")
    return None


def _destination_from_current_repo(cwd: Path | None) -> Destination | None:
    """Resolve a flagless create/comment command's target from the git remote."""
    if cwd is None:
        return None
    slug = slug_for_cwd(cwd)
    return Destination(slug=slug, via="cwd") if slug else None


def _destination_from_forge_url(words: list[str]) -> Destination | None:
    """Resolve the target from the FIRST forge URL positional in ``words``, or ``None``.

    ``gh issue comment https://github.com/owner/repo/issues/5`` names its target
    by URL with no ``--repo`` flag; the slug is the path before the resource
    segment. This is more specific than the cwd remote, so it is resolved before
    the current-repo fallback.
    """
    for word in words:
        match = _FORGE_URL_SLUG_RE.search(word)
        if match:
            return Destination(slug=match.group("slug").removesuffix(".git"), via="url")
    return None


def _flagless_destination(words: list[str], tool: str, cwd: Path | None) -> Destination | None:
    """Resolve a publish target when no explicit ``--repo``/``-R`` flag is present.

    Priority: a raw-REST ``api`` URL path, then the ``gh`` ``GH_REPO`` env
    default, then a forge URL positional in the args, then the current repo for a
    create/comment/note verb. ``None`` when none of these resolves a target.
    """
    if "api" in words:
        return _destination_from_api(words, tool)
    if tool == "gh" and os.environ.get("GH_REPO", "").strip():
        return Destination(slug=os.environ["GH_REPO"].strip(), via="env")
    url_dest = _destination_from_forge_url(words)
    if url_dest is not None:
        return url_dest
    if len(words) >= 3 and (words[1], words[2]) in _CURRENT_REPO_VERBS:  # noqa: PLR2004
        return _destination_from_current_repo(cwd)
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
        return Destination(slug=explicit, via="flag")
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


def _internal_publish_namespaces(config_path: Path | None = None) -> list[str]:
    """Return the ``[teatree] internal_publish_namespaces`` allowlist (lower-cased).

    The list of host/namespace prefixes that are PROVABLY internal. Read
    from the ``T3_INTERNAL_PUBLISH_NAMESPACES`` env var first (comma- or
    space-separated, for a quick per-session override), then the
    ``[teatree] internal_publish_namespaces`` key in ``~/.teatree.toml``.
    DEFAULT is empty -- every destination stays PUBLIC, so behaviour is
    unchanged for users who have not configured the allowlist.

    No real company/customer namespace is hardcoded here; the allowlist
    lives only in the user's private config / env.
    """
    env_raw = os.environ.get("T3_INTERNAL_PUBLISH_NAMESPACES", "")
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
        entries = teatree.get("internal_publish_namespaces", []) if isinstance(teatree, dict) else []
        if isinstance(entries, list):
            toml_entries = [str(e).strip().lower() for e in entries if str(e).strip()]
    return env_entries + toml_entries


def _namespace_matches(entry: str, slug: str) -> bool:
    """Return True iff allowlist ``entry`` matches ``slug`` on segment boundaries.

    The match is a path-segment prefix: ``entry`` must equal ``slug`` or be
    a leading run of its ``/``-separated segments (``a/b`` matches ``a/b`` and
    ``a/b/c`` but not ``a/bc``). This keeps ``internalcorp`` from matching an
    unrelated ``internalcorp-public/repo`` while still covering every repo
    under a configured namespace.
    """
    if entry == slug:
        return True
    entry_parts = entry.split("/")
    slug_parts = slug.split("/")
    return len(entry_parts) < len(slug_parts) and slug_parts[: len(entry_parts)] == entry_parts


def is_public_destination(dest: Destination | None, *, config_path: Path | None = None) -> bool:
    """Return True iff ``dest`` should be treated as a PUBLIC publish target.

    FAIL-CLOSED classification: a destination is PUBLIC (the gate scans and
    blocks) UNLESS it is PROVABLY internal. A destination is internal when ANY
    of these resolves its slug to private:

    - the ``[teatree] internal_publish_namespaces`` /
        ``T3_INTERNAL_PUBLISH_NAMESPACES`` allowlist, as a case-insensitive
        prefix-SEGMENT match (``internalcorp`` matches ``internalcorp/svc``
        and ``host/internalcorp/svc`` but not ``other/internalcorp-public``);
    - the existing ``[teatree] private_repos`` allowlist that the
        commit / pure-post carve-out already consults
        (:func:`_repo_visibility.slug_is_allowlisted_private`, a
        case-insensitive SUBSTRING match), so a user's CURRENT
        ``private_repos`` config makes their private namespaces skip the
        public-leak scan without maintaining a second allowlist;
    - the day-cached ``gh``/``glab`` live-visibility probe
        (:func:`_repo_visibility.slug_is_private`), the same fallback the
        commit / pure-post carve-out (:func:`publish_surface.segment_target_is_private`)
        already applies to its command-resolved target. Resolving visibility
        from the COMMAND's target slug (the ``--repo``/``-R`` flag, the
        ``api`` URL path, or the cwd remote) rather than the harness cwd is
        what lets a post FROM a public clone TO a provably-private repo skip
        the public-leak scan instead of over-blocking. The probe returns
        ``None`` (unknown -- tool absent in-hook or auth differs) for an
        unresolvable target, which stays PUBLIC.

    A ``None`` destination (unresolvable target) is PUBLIC, and a probe that
    cannot prove the target private leaves it PUBLIC -- detection failure
    never weakens the gate.
    """
    if dest is None:
        return True
    slug = dest.slug.strip().lower()
    if not slug:
        return True
    if any(_namespace_matches(entry, slug) for entry in _internal_publish_namespaces(config_path)):
        return False
    if slug_is_allowlisted_private(slug, config_path):
        return False
    return not slug_is_private(slug)


def gate_skips_destination(command: str, cwd: Path | None, *, config_path: Path | None = None) -> bool:
    """Return True iff a publish-surface gate should SKIP scanning ``command``.

    The banned-terms / bare-reference gates scan only PUBLIC targets. The
    skip is the ALL-SEGMENTS inversion that mirrors
    :func:`publish_surface.command_is_pure_private_gh_glab_post`: skip ONLY
    when EVERY top-level segment is provably safe to skip and there is at
    least one publish segment. A single public, unresolvable, ``api``, or
    substitution/transport-carrying segment makes the WHOLE command scan
    (fail-closed). Otherwise a chained or substituted public post hides
    behind a leading internal segment and is never scanned.

    A segment is skip-safe when it is one of:

    - a publish segment whose destination resolves to a provably-INTERNAL
        repo/namespace, which carries no substitution/transport construct; or
    - a segment that is PROVABLY a recognised navigation / local-only /
        git-transport command (:func:`_segment_is_skip_inert` -- its leading
        executable is in the closed ``_SKIP_INERT_LEADERS`` allowlist, e.g.
        ``cd``/``echo``/``git push``, with no forge token or
        substitution/transport construct).

    Every OTHER segment is NOT skip-safe and makes the whole command scan
    (fail-closed): a raw ``gh api`` / ``glab api`` call, a
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
        if _segment_carries_substitution_or_transport(words) or _segment_is_api_call(words):
            return False
        dest = _destination_from_words(words, cwd)
        if dest is not None:
            if is_public_destination(dest, config_path=config_path):
                return False
            saw_internal_publish = True
        elif not _segment_is_skip_inert(words):
            return False
    return saw_internal_publish
