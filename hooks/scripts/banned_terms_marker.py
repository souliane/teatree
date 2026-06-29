"""Destination-aware resolution of a banned-terms fail-closed MARKER (#1415).

Split out of ``hook_router.py`` (a shrink-only module-health-capped god-module)
so the marker decision's logic and its rationale live in a bare sibling module
the router imports. The router keeps only the thin ``deny`` emission; this module
owns "given a fail-closed marker, does the destination let it DOWNGRADE to a warn,
or does it hard-block?".

``scan_text`` returns either a configured banned term or one of three fail-closed
markers. A REAL configured term is handled by the router's destination-aware
banned-term path, not here -- ``resolve_marker`` reports ``is_marker=False`` for
it. For the two unreadable-body markers the decision is destination-aware (the
SCANNER-unavailable marker stays hard-blocking on every surface, #1954). It
downgrades to a warn in two cases. First, ANY local ``git commit`` -- a landing
repo of ANY visibility, PUBLIC included (``command_targets_local_commit``, #1415):
a commit is LOCAL, and the pre-push public-leak gate
(``refuse-public-push-with-leak.sh``, #703) re-scans EVERY commit message in the
push range for banned terms before they reach a public remote, so an ordinary
commit whose body the gate cannot READ at scan time (a ``-F -`` stdin / heredoc /
``-m "$VAR"`` body) must not hard-block -- that over-block stuck multiple coders
mid-commit. Second, a pure private ``gh``/``glab`` post
(``command_targets_private_only``): not a public surface at all. A non-commit
PUBLIC ``gh``/``glab`` post is the real public action with no push gate behind it,
so it is NOT widened -- it keeps hard-blocking, and the chained-segment proof
inside ``command_targets_local_commit`` keeps a commit chained to such a post
hard-blocked too.
"""

from dataclasses import dataclass
from pathlib import Path

_PRIVATE_DEST_WARNING = (
    "WARNING: banned-terms gate (#1415) — could not read the commit/post body, but the "
    "destination is a private repo; downgraded to warn. A private-repo body is not a public leak.\n"
)
_LOCAL_COMMIT_WARNING = (
    "WARNING: banned-terms gate (#1415) — could not read the commit body, but it is a LOCAL "
    "commit; downgraded to warn. The pre-push gate re-scans commit messages for banned terms "
    "before they reach a public remote.\n"
)


@dataclass(frozen=True)
class MarkerVerdict:
    """Outcome of resolving a ``scan_text`` result against the destination.

    ``deny_message`` is the hard-block reason (the marker's own deny message) when
    the marker must DENY; ``warning`` is the stderr line to print when it
    DOWNGRADES to a warn. Exactly one is non-``None`` for a fail-closed marker;
    BOTH are ``None`` for a real configured banned term (the router takes its
    own destination-aware banned-term path) -- distinguished by ``is_marker``.
    """

    deny_message: str | None
    warning: str | None
    is_marker: bool


def resolve_marker(term: str, command: str, cwd_repo: Path | None) -> MarkerVerdict:
    """Resolve a ``scan_text`` result to a deny / downgrade / not-a-marker verdict."""
    from teatree.hooks import banned_terms_scanner, publish_surface  # noqa: PLC0415

    marker_message = banned_terms_scanner.marker_deny_message(term)
    if marker_message is None:
        return MarkerVerdict(deny_message=None, warning=None, is_marker=False)
    unreadable_body_markers = {
        banned_terms_scanner.UNRESOLVABLE_BODY_MARKER,
        banned_terms_scanner.UNAVAILABLE_BODY_SOURCE_MARKER,
    }
    if term in unreadable_body_markers and publish_surface.command_targets_local_commit(command, cwd_repo):
        return MarkerVerdict(deny_message=None, warning=_LOCAL_COMMIT_WARNING, is_marker=True)
    if term in unreadable_body_markers and publish_surface.command_targets_private_only(command, cwd_repo):
        return MarkerVerdict(deny_message=None, warning=_PRIVATE_DEST_WARNING, is_marker=True)
    return MarkerVerdict(deny_message=marker_message, warning=None, is_marker=True)
