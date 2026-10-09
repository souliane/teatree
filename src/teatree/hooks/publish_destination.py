"""Resolve publish targets and classify their visibility for leak gates.

Commands may name a repo through ``--repo``, a forge API path, ``GH_REPO``,
a work-item URL, or the current git remote. Resolved targets keep their host
or inherit the command's forge before the shared private-repo verdict runs.
An unknown target is scanned. A reachable PUBLIC forge answer always wins over
a ``private_repos`` declaration.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final
from urllib.parse import urlparse

from teatree.hooks import _repo_visibility
from teatree.hooks._commit_repo_dir import NAVIGATION_VERBS, segment_cwds
from teatree.hooks._forge_write_detection import curl_forge_targets
from teatree.hooks._gh_glab_hiding import command_segments, raw_has_live_substitution, token_is_transport_construct
from teatree.hooks._private_repo_entries import private_repo_visibility, qualified_repo_slug
from teatree.hooks._python_rest_detection import find_python_forge_rest_urls, is_python_leader
from teatree.hooks.publish_surface import (
    _GH_ELIGIBLE_VERBS,
    _GLAB_ELIGIBLE_VERBS,
    _extract_repo_flag,
    _segment_is_publish_inert,
    _strip_benign_prefix,
)
from teatree.utils.url_slug import project_slug_from_ref


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
    :func:`is_public_destination`, which forwards it to the shared visibility
    rule. A host-qualified slug already
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

# A leading ``https?://<host>/`` and an ``api/vN/`` REST-version segment that a
# full-URL or version-prefixed endpoint carries before the ``repos/`` /
# ``projects/`` path the slug patterns above match. ``glab``/``gh api`` accept
# the endpoint as a bare relative path (``projects/...``), a version-prefixed
# path (``api/v4/projects/...``), or a full URL
# (``https://gitlab.com/api/v4/projects/...``); stripping this optional prefix
# normalises all three to the bare relative form. Both groups are optional, so a
# bare ``[/]repos/...`` / ``[/]projects/...`` endpoint is returned unchanged.
_API_ENDPOINT_PREFIX_RE: Final[re.Pattern[str]] = re.compile(r"^(?:https?://[^/]+/)?(?:/?api/v\d+/)?")

# The ``owner/repo`` of a forge URL positional, before the resource segment
# (GitLab ``/-/`` infix and nested group paths handled; ``.git`` suffix stripped).
_FORGE_URL_SLUG_RE: Final[re.Pattern[str]] = re.compile(
    r"https?://(?:[^/@\s]+@)?(?P<host>[\w.-]+\.[\w.-]+)(?::\d+)?/"
    r"(?P<slug>[\w.-]+(?:/[\w.-]+)+?)"
    r"(?:/(?:-/)?(?:issues|pull|pulls|merge_requests|commit|tree|blob)\b|\.git\b|/?$)",
)

# ``gh``/``glab`` create/comment verbs whose target, when no ``--repo``/``-R``
# flag is present, is the CURRENT repo (resolved from the git remote).
# ``("pr", "review")`` is included so a flagless ``gh pr review 5 --body …``
# (an approve/request-changes body that publishes a HIGH verbatim quote) resolves
# to the current repo and is visibility-classified rather than treated as an
# unresolved destination the leak gate skipped (#F7.2).
_CURRENT_REPO_VERBS: Final[frozenset[tuple[str, str]]] = (
    _GH_ELIGIBLE_VERBS | _GLAB_ELIGIBLE_VERBS | frozenset({("pr", "review")})
)

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
# provably skip-safe: navigation / local-only / git-transport commands, plus the
# read-only pipe filters agents chain after a forge command to suppress its
# pager -- none can make a network call or spawn another command. CLOSED
# POSITIVE allowlist, not a denylist: an UNRECOGNISED leader (``make``,
# ``python``, ``./release.sh``, an ``ssh``/``xargs`` wrapper -- and deliberately
# ``sed``/``awk``, whose ``e``/``system()`` execute) can shell out to
# ``gh``/``curl`` with no forge token in its own argv, so it is NOT provably
# inert and the whole command fails closed (scans). ``git`` is included because
# a ``git push`` carries commits to a git remote -- the COMMIT gate's surface,
# mirroring the commit chain's treatment of ``git push`` as publish-inert.
_SKIP_INERT_LEADERS: Final[frozenset[str]] = frozenset(
    {"cd", "pushd", "popd", "cat", "echo", "printf", "true", ":", "git"}
    | {"head", "tail", "grep", "wc", "sort", "uniq", "cut", "tr", "tee", "jq"}
)


def _forge_for_tool(tool: str) -> str:
    """Map a publish tool word to its forge: ``gh`` -> github, ``glab`` -> gitlab.

    The forge is what lets the visibility probe pick ``gh`` vs ``glab`` for a
    BARE ``owner/repo`` slug that carries no host segment of its own. An
    unrecognised leader yields ``""`` (no hint; the probe falls back to the
    slug's own host or the GitHub default).
    """
    return {"gh": "github", "glab": "gitlab"}.get(tool, "")


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


def _normalize_api_endpoint(url: str) -> str:
    """Strip a leading ``https?://<host>/`` and ``api/vN/`` prefix from an endpoint.

    The ``repos/...`` / ``projects/...`` slug patterns match a RELATIVE endpoint,
    but ``gh``/``glab api`` also accept a version-prefixed (``api/v4/projects/...``)
    or full-URL (``https://gitlab.com/api/v4/projects/...``) endpoint. Removing the
    optional host + ``api/vN/`` prefix collapses all three forms to the bare
    relative path the patterns expect; a bare endpoint is returned unchanged.
    """
    return _API_ENDPOINT_PREFIX_RE.sub("", url, count=1)


def _destination_from_api(words: list[str], tool: str) -> Destination | None:
    """Resolve the destination of a ``gh api`` / ``glab api`` raw REST call."""
    url = _api_url_arg(words)
    if url is None:
        return None
    host = urlparse(url).hostname if url.startswith(("http://", "https://")) else None
    if host == "api.github.com":
        host = "github.com"
    url = _normalize_api_endpoint(url)
    forge = _forge_for_tool(tool)
    if tool == "gh":
        match = _GH_API_REPOS_RE.match(url)
        if not match:
            return None
        slug = match.group(1)
        return Destination(slug=f"{host}/{slug}" if host else slug, via="api", forge=forge)
    match = _GLAB_API_PROJECTS_RE.match(url)
    if match:
        project = match.group(1).replace("%2F", "/").replace("%2f", "/")
        return Destination(
            slug=f"{host}/{project}" if host else project,
            via="api",
            forge=forge,
        )
    return None


def _destination_from_current_repo(cwd: Path | None, forge: str) -> Destination | None:
    """Resolve a flagless create/comment command's target from the git remote.

    ``cwd`` is the SEGMENT's own publish dir (:func:`_commit_repo_dir.segment_cwds`),
    not the raw ambient one.
    """
    if cwd is None:
        return None
    slug = _repo_visibility.slug_for_cwd(cwd)
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
            return Destination(
                slug=f"{match.group('host')}/{match.group('slug').removesuffix('.git')}", via="url", forge=forge
            )
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
    if len(words) >= 3 and (words[1], words[2]) in _CURRENT_REPO_VERBS:  # noqa: PLR2004 — self-documenting literal in this context
        return _destination_from_current_repo(cwd, forge)
    return None


# ``t3 [overlay] review post-comment`` -- the
# GitLab-only review-post verbs. The FIRST positional after the verb is the
# project slug (confirmed at ``cli/review/commands.py`` ``post_comment``:
# ``repo`` is the leading ``typer.Argument``).
_T3_REVIEW_POST_VERBS: Final[frozenset[str]] = frozenset({"post-comment"})


def _first_positional(words: list[str]) -> str | None:
    """Return the first token in ``words`` that is not a flag, or ``None``.

    A flag is any token starting with ``-`` (``--body-file``, ``--live``, ``-m``).
    Taking the first NON-FLAG token rather than strictly the first one tolerates
    an interleaved leading flag (``--live <repo>``) before the repo positional.
    """
    for word in words:
        if word.startswith("-"):
            continue
        return word
    return None


def _destination_from_t3_review(words: list[str]) -> Destination | None:
    """Resolve the destination of a ``t3 [overlay] review post-comment``.

    ``t3 review`` posts a GitLab MR comment / draft note on the user's behalf; its
    destination is the project-slug positional. The resolver never extracted it
    (``_destination_from_words`` only knew ``gh``/``glab``), so a ``t3``-led
    segment resolved to no destination -- and ``t3`` is not a recognised inert
    leader -- so a post to an allowlisted-private repo fell through to the
    fail-closed leak scan and over-fired.

    The leader is canonicalised up to the ``t3`` basename so a path-form leader
    (``./t3``, ``/usr/local/bin/t3``) is recognised the same as a bare ``t3``, and
    the arbitrary overlay token between ``t3`` and ``review`` is tolerated --
    mirroring :func:`_command_parser._segment_is_t3_publish`. The forge is pinned
    to ``gitlab`` because ``t3 review`` is GitLab-only.
    """
    if PurePosixPath(words[0]).name != "t3":
        return None
    for i in range(1, len(words) - 1):
        if words[i] == "review" and words[i + 1] in _T3_REVIEW_POST_VERBS:
            slug = _first_positional(words[i + 2 :])
            return Destination(slug=slug, via="t3", forge="gitlab") if slug else None
    return None


def _destination_from_rest_url(words: list[str]) -> Destination | None:
    """Resolve the destination of a python REST script or a ``curl`` from its URL literal.

    Reuses :func:`_publish_detection.find_python_forge_rest_urls` -- the SAME
    ``repos/<owner>/<repo>`` (GitHub) / ``api/v<N>/projects/<slug>`` (GitLab)
    path resolution :func:`_destination_from_api` applies to a ``gh``/``glab
    api`` URL argument, now applied to a URL LITERAL embedded in the script
    text or named by ``curl``. Resolution is orthogonal to read/write (mirrors
    ``_destination_from_api``, which resolves a ``gh api ... --method GET``
    target too) -- the write/read gate is upstream of this resolver. A
    dynamically-built URL (string concatenation) carries no literal
    ``https?://`` substring and resolves to ``None`` -- genuinely unresolvable,
    not private.
    """
    if not words:
        return None
    if is_python_leader(words[0]):
        targets = list(find_python_forge_rest_urls(" ".join(words[1:])))
    else:
        targets = [target for target in curl_forge_targets(words) if target is not None]
    for forge, slug in targets:
        return Destination(slug=slug, via="api", forge=forge)
    return None


def _destination_from_words(words: list[str], cwd: Path | None) -> Destination | None:
    """Resolve the publish destination of one command segment's word list.

    The visibility-independent half of :func:`resolve_publish_destination`,
    factored out so :func:`public_visibility.gate_skips_for_visibility` can
    resolve a destination PER top-level segment (the ALL-SEGMENTS invariant)
    rather than only from the first segment.
    """
    if not words:
        return None
    t3_dest = _destination_from_t3_review(words)
    if t3_dest is not None:
        return t3_dest
    rest_dest = _destination_from_rest_url(words)
    if rest_dest is not None:
        return rest_dest
    if words[0] not in {"gh", "glab"}:
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
    - ``gh api [https://<host>/][api/vN/]repos/<owner>/<repo>/...`` -- the
        ``repos/`` path segment, after stripping an optional full-URL host and
        ``api/vN/`` REST-version prefix.
    - ``glab api [https://<host>/][api/vN/]projects/<url-encoded ns%2Frepo>/...``
        -- the ``projects/`` path segment, ``%2F``-decoded, after the same
        host + ``api/vN/`` prefix strip.
    - ``t3 [overlay] review post-comment <ns>/<repo> ...``
        -- the project-slug positional (forge pinned to gitlab; ``t3 review`` is
        GitLab-only).
    - ``gh``/``glab`` ``pr``/``issue``/``mr`` ``create``/``comment``/``note``
        with no ``--repo`` flag -- the CURRENT repo, via the git remote of THAT
        segment's own publish dir (:func:`_commit_repo_dir.segment_cwds`).
    - a ``python3``/``python``-led REST-publish script or a ``curl`` -- the SAME
        ``repos/``/``projects/`` path shape, resolved from a URL LITERAL
        (:func:`_destination_from_rest_url`) instead of a CLI flag/positional.

    Resolves the first segment that is not a ``cd``/``pushd`` navigation, against
    the dir bash would run THAT segment in (:func:`_commit_repo_dir.segment_cwds`)
    -- reading segment ZERO would read the ``cd`` itself in exactly the case the
    two dirs differ, making the cwd fallback unreachable where it is needed.
    :func:`public_visibility.gate_skips_for_visibility` is the multi-segment
    predicate. Returns ``None`` when the target cannot be determined (a
    non-publish command, a ``curl``/Slack surface, a flagless API call, or a
    flagless create with no resolvable git remote). ``None`` is the fail-closed
    signal for :func:`is_public_destination` (treat as PUBLIC) and the
    fail-open signal for the affirmative-public scope (treat as NON-public).
    """
    for words, segment_cwd in zip(command_segments(command), segment_cwds(command, cwd), strict=True):
        if words[0] not in NAVIGATION_VERBS:
            return _destination_from_words(words, segment_cwd)
    return None


def _segment_carries_substitution_or_transport(words: list[str], raws: list[str]) -> bool:
    """Return True iff any token is a LIVE substitution or a transport construct.

    A ``$(...)`` / backtick / process-substitution that bash would EXPAND, or a
    redirection/here-doc/group-opener token, can run a SECOND command (a public
    post) when the shell processes the line -- so the gate must NOT skip and must
    scan instead.

    The substitution half reads each token's as-written source span (``raws``,
    index-aligned with ``words``) via :func:`raw_has_live_substitution` rather than
    its decoded value: a marker inside a SINGLE-quoted body value is inert literal
    text bash passes verbatim (``--body 'name the `flag` here'``), so it cannot
    launch a second command and must NOT force a scan on an otherwise
    private-target post (#3357). A marker that is unquoted or inside DOUBLE quotes
    still expands, so it still scans -- the exact live-versus-inert distinction the
    sibling opaque-transport check already makes. An empty raw span fails closed
    (treated as live). The transport-construct half stays on the decoded ``words``,
    where it belongs.
    """
    if any(raw_has_live_substitution(raw) for raw in raws):
        return True
    return any(token_is_transport_construct(token) for token in words)


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


def _host_relative_slug(slug: str) -> str:
    """Keep the host when reducing a full issue or merge-request URL."""
    if not slug.startswith(("http://", "https://")):
        return slug
    parsed = urlparse(slug)
    project = project_slug_from_ref(slug) or parsed.path.strip("/")
    return f"{parsed.hostname}/{project}" if parsed.hostname and project else ""


def is_public_destination(dest: Destination | None, *, config_path: Path | None = None) -> bool:
    """Treat a destination as public unless its real host is proven private.

    Full issue/PR URLs retain their host while their path is reduced to the repo.
    Bare slugs inherit ``dest.forge`` before the shared host-qualified visibility
    decision. A reachable PUBLIC probe overrides ``private_repos``; an unknown
    probe leaves a matching declaration private. Empty or unresolved targets
    stay public so outbound leak checks run.
    """
    if dest is None:
        return True
    slug = dest.slug.strip().lower()
    if not slug or "$" in slug:
        return True
    slug = _host_relative_slug(slug)
    if not slug:
        return True
    probe_slug = qualified_repo_slug(slug, dest.forge, ops=_repo_visibility)
    return private_repo_visibility(
        probe_slug,
        config_path,
        ops=_repo_visibility,
    ) not in {"PRIVATE", "INTERNAL"}
