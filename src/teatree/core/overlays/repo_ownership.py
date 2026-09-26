"""Which registered overlay owns a forge repo slug (#3793 seam).

Two surfaces need the same answer from different layers: the review surface derives a
post's forge base URL and API token from it
(:mod:`teatree.cli.review.forge_target`), and the publication privacy gate derives the
overlay whose rules attribute a public-target scan
(:mod:`teatree.core.gates.privacy_gate`). ``teatree.core`` cannot reach up into
``teatree.cli``, so the resolution lives here and the CLI delegates.

Resolution is by ENUMERATED repo identity first
(:func:`~teatree.core.overlay_loader.infer_overlay_for_url`, which matches each
overlay's ``get_workspace_repos()``), then by DECLARED forge namespace
(:func:`~teatree.core.overlays.overlay_namespace.namespace_owner`). Enumeration alone
leaves a repo created in a group an overlay owns but never added to its table resolving
to nothing — and on a multi-overlay install nothing means "ask the ambient overlay",
which has no claim on the target.
"""

import logging

from teatree.core.overlay_loader import OverlayConfigResolver, infer_overlay_for_url
from teatree.core.overlays.overlay_namespace import namespace_owner

logger = logging.getLogger(__name__)


def owning_overlay_for_repo(repo: str, *, forge: str) -> str:
    """The overlay that owns *repo* on *forge*, or ``""`` when it is not exactly one.

    ``""`` for an unowned or ambiguously-owned slug, and for a registry whose declared
    scopes will not read — never a guess. A caller treats it as "resolve ambiently",
    which is what it had before asking.
    """
    if (scopes := _declared_scopes()) is None:
        return ""
    slug = repo.strip()
    return infer_overlay_for_url(slug) or namespace_owner(slug, scopes, forge=forge)


def _declared_scopes() -> list[tuple[str, dict[str, list[str]]]] | None:
    """``(overlay, owned_repos)`` for every registered overlay, or ``None`` when one will not read.

    An unreadable scope is not an absent one: dropping it can collapse a safe TIE
    into a single owner, and that owner then supplies the token and base URL the
    post is addressed with. So the whole attribution declines — the target reads
    as unowned — rather than the failed read silently picking a winner. Still never
    fatal: the failure is warned, not raised.
    """
    scopes: list[tuple[str, dict[str, list[str]]]] = []
    for name in OverlayConfigResolver.all_names():
        try:
            scopes.append((name, OverlayConfigResolver.owned_repos(name)))
        except Exception:
            logger.warning("Overlay %r owned_repos read failed while attributing a target repo", name, exc_info=True)
            return None
    return scopes
