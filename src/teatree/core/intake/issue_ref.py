"""Canonicalize the ``workspace ticket`` positional into a full issue URL.

The recorded bug: ``t3 <overlay> workspace ticket 3274`` accepted the bare
number and stored ``issue_url='3274'`` verbatim. Every downstream derivation
that expects a real forge URL then drifts — the overlay resolves the overlay
name to ``''`` and the branch/URL fall back to garbage, producing malformed
duplicate tickets. This module refuses or canonicalizes a non-URL argument at
the ``workspace ticket`` boundary so a malformed ticket can never be created:
a bare number is resolved against the overlay's OWN repo to a full URL, or
rejected with an actionable message.
"""

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from teatree.core.overlay import OverlayBase

_BARE_NUMBER = re.compile(r"^#?(\d+)$")


class InvalidIssueRefError(ValueError):
    """The ``workspace ticket`` argument was neither a URL nor a resolvable issue number."""


def canonicalize_issue_ref(overlay: "OverlayBase", raw: str) -> str:
    """Return a full issue URL for *raw*, resolving a bare number against *overlay*'s repo.

    A real forge URL (``https://…`` / ``…://…``) or an ``owner/repo#n`` cross-repo
    slug passes through unchanged. A bare ``3274`` / ``#3274`` is resolved via
    ``overlay.resolve_issue_token`` (the overlay's code host + its first repo's
    git remote) to a canonical ``…/issues/3274`` URL. An empty argument, or a
    bare number the overlay cannot resolve to a URL, raises
    :class:`InvalidIssueRefError` so the caller refuses rather than persisting a
    malformed ``issue_url``.
    """
    ref = raw.strip()
    if not ref:
        empty = "workspace ticket needs an issue URL or number; got an empty argument."
        raise InvalidIssueRefError(empty)
    if "://" in ref or "/" in ref:
        return ref
    match = _BARE_NUMBER.match(ref)
    if match is None:
        return ref
    number = int(match.group(1))
    url = overlay.resolve_issue_token(number)
    if url:
        return url
    unresolved = (
        f"{raw!r} is a bare issue number, but the overlay's repo could not be resolved to a full "
        f"URL. Pass the full issue URL (e.g. https://github.com/<owner>/<repo>/issues/{number})."
    )
    raise InvalidIssueRefError(unresolved)
