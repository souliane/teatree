"""Destination-aware gate skip for the pre-publish gates (publish-surface purpose).

The banned-terms (#1415) and bare-reference (#1530) gates exist to stop
leaks on PUBLIC surfaces. Firing on EVERY publish command -- including
writes to an INTERNAL/PRIVATE repo or namespace -- over-blocks: a private
repo's own customer/domain terms and bare cross-references are exactly
what its issues/PRs are supposed to carry.

:func:`resolve_publish_destination` extracts the target repo/namespace
from a publish command and :func:`is_public_destination` classifies it
FAIL-CLOSED: a destination is PUBLIC (gate scans/blocks) UNLESS it is
PROVABLY internal -- its namespace matches the CONFIG-DRIVEN ``[teatree]
internal_publish_namespaces`` allowlist (or the
``T3_INTERNAL_PUBLISH_NAMESPACES`` env var). The default when the key is
absent is an empty allowlist, so every destination stays PUBLIC and
behaviour is UNCHANGED for unconfigured users. An unresolvable destination
is PUBLIC (scan). :func:`gate_skips_destination` is the composed predicate
the gates call.

The shared command-parsing helpers (``_extract_repo_flag``,
``_slug_for_cwd``, ``_config_path``, the eligible-verb sets) live in
:mod:`teatree.hooks.publish_surface`; this module reuses them so the
repo-target resolution stays in one place across both the private-repo
carve-out and the destination skip.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from teatree.hooks._command_parser import first_segment_words
from teatree.hooks.publish_surface import (
    _GH_ELIGIBLE_VERBS,
    _GLAB_ELIGIBLE_VERBS,
    _config_path,
    _extract_repo_flag,
    _slug_for_cwd,
)


@dataclass(frozen=True)
class Destination:
    """The target a publish command writes to.

    ``slug`` is the repo/namespace as it appears on the command line
    (``owner/repo``, ``host/owner/repo``, or ``ns/sub/repo``). ``via``
    records how it was resolved (``flag`` for ``--repo``/``-R``, ``api`` for
    a ``gh``/``glab api`` URL path, ``cwd`` for the current-repo fallback) so
    a caller can log the provenance without re-parsing.
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

# ``gh``/``glab`` create/comment verbs whose target, when no ``--repo``/``-R``
# flag is present, is the CURRENT repo (resolved from the git remote).
_CURRENT_REPO_VERBS: Final[frozenset[tuple[str, str]]] = _GH_ELIGIBLE_VERBS | _GLAB_ELIGIBLE_VERBS

# Flags whose VALUE is the next token in a ``gh``/``glab api`` call -- skipped
# (flag + value) so the bare endpoint URL is found regardless of ordering.
_API_VALUE_FLAGS: Final[frozenset[str]] = frozenset(
    {"-f", "-F", "--field", "--raw-field", "-X", "--method", "--input", "-H", "--header"}
)


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
    slug = _slug_for_cwd(cwd)
    return Destination(slug=slug, via="cwd") if slug else None


def _flagless_destination(words: list[str], tool: str, cwd: Path | None) -> Destination | None:
    """Resolve a publish target when no explicit ``--repo``/``-R`` flag is present.

    Priority: a raw-REST ``api`` URL path, then the ``gh`` ``GH_REPO`` env
    default, then the current repo for a create/comment/note verb. ``None``
    when none of these resolves a target.
    """
    if "api" in words:
        return _destination_from_api(words, tool)
    if tool == "gh" and os.environ.get("GH_REPO", "").strip():
        return Destination(slug=os.environ["GH_REPO"].strip(), via="env")
    if len(words) >= 3 and (words[1], words[2]) in _CURRENT_REPO_VERBS:  # noqa: PLR2004
        return _destination_from_current_repo(cwd)
    return None


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

    Returns ``None`` when the target cannot be determined (a non-publish
    command, a ``curl``/Slack surface, a flagless API call, or a flagless
    create with no resolvable git remote). ``None`` is the caller's signal
    to treat the destination as PUBLIC and scan (fail-closed).
    """
    words = first_segment_words(command)
    if not words or words[0] not in {"gh", "glab"}:
        return None
    explicit = _extract_repo_flag(words)
    if explicit:
        return Destination(slug=explicit, via="flag")
    return _flagless_destination(words, words[0], cwd)


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
    blocks) UNLESS it is PROVABLY internal. "Provably internal" means its
    slug matches the ``[teatree] internal_publish_namespaces`` /
    ``T3_INTERNAL_PUBLISH_NAMESPACES`` allowlist as a case-insensitive
    prefix-segment match (``internalcorp`` matches ``internalcorp/svc`` and
    ``host/internalcorp/svc`` but not ``other/internalcorp-public``).

    A ``None`` destination (unresolvable target) is PUBLIC -- detection
    failure never weakens the gate.
    """
    if dest is None:
        return True
    slug = dest.slug.strip().lower()
    if not slug:
        return True
    return not any(_namespace_matches(entry, slug) for entry in _internal_publish_namespaces(config_path))


def gate_skips_destination(command: str, cwd: Path | None, *, config_path: Path | None = None) -> bool:
    """Return True iff a publish-surface gate should SKIP scanning ``command``.

    The banned-terms / bare-reference gates scan only PUBLIC targets. They
    skip when the destination RESOLVES to a provably-internal namespace. A
    ``None`` (unresolvable) destination, or a public one, is scanned --
    fail-closed.
    """
    dest = resolve_publish_destination(command, cwd)
    if dest is None:
        return False
    return not is_public_destination(dest, config_path=config_path)
