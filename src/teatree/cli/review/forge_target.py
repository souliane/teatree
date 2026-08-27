"""Where a review post is addressed, and with what credential.

``t3 review <cmd> <repo> <mr> …`` names its target in the invocation, so both
coordinates a post needs — the forge base URL and the API token — are derivable from
that slug: the overlay that owns the repo carries them. Resolving them from the
AMBIENT overlay instead makes the whole surface conditional on how many overlays
happen to be registered, because ``get_overlay()`` raises ``Multiple overlays found``
as soon as there is more than one and no explicit pin (souliane/teatree#3793).

Both reads go through the same owning overlay, so a post can never be addressed to one
forge with another's credential.

A read that FAILED is kept distinct from one that found nothing
(:class:`~teatree.cli.review.guarded_read.ReadOutcome`): the two need different
remediation, and reporting the first as the second sends the operator to a re-login
that changes nothing (souliane/teatree#3794).
"""

import logging
import os
from typing import TYPE_CHECKING

from teatree.cli.review.guarded_read import ReadOutcome, guarded_read, read_or_refuse
from teatree.utils.run import run_allowed_to_fail

if TYPE_CHECKING:
    from teatree.core.overlay import OverlayBase

logger = logging.getLogger(__name__)

_CRED_READ = "the review API token from the overlay that owns the target repo"
_URL_READ = "the review GitLab base URL from the overlay config"

# ``t3 review`` posts only to GitLab (``send_routing.route_forge_send`` pins the
# same forge), so only GitLab-hosted namespace declarations may attribute a target.
_REVIEW_FORGE = "gitlab"


def owning_overlay_name(repo: str) -> str:
    """The overlay that owns *repo* — by enumerated repo identity, then by declared namespace.

    :func:`~teatree.core.overlay_loader.infer_overlay_for_url` matches an
    ENUMERATION (each overlay's ``get_workspace_repos()``), so a repo created in a
    group the overlay owns but never added to its table resolves to nothing — and
    on a multi-overlay install nothing means "ask the ambient overlay", which has
    no claim on the target. The declared forge namespace
    (:func:`~teatree.core.overlays.overlay_namespace.namespace_owner`) answers for
    the group.

    That fallback is applied HERE rather than inside ``infer_overlay_for_url``
    because ``owned_repos`` "gates ONLY the unknown-repo approval decision, never
    merge-without-review" (:class:`~teatree.core.overlay.OverlayConfig`) and that
    resolver also feeds merge authorization — so the namespace declaration reaches
    the review surface it was added for and nothing else.

    ``""`` when no overlay owns *repo*, when more than one does, or when any
    registered overlay's declared scope will not read.
    """
    from teatree.core.overlay_loader import infer_overlay_for_url  # noqa: PLC0415 — deferred: keeps CLI startup light
    from teatree.core.overlays.overlay_namespace import (  # noqa: PLC0415 — deferred: keeps CLI startup light
        namespace_owner,
    )

    if (scopes := _declared_scopes()) is None:
        return ""
    slug = repo.strip()
    return infer_overlay_for_url(slug) or namespace_owner(slug, scopes, forge=_REVIEW_FORGE)


def _declared_scopes() -> list[tuple[str, dict[str, list[str]]]] | None:
    """``(overlay, owned_repos)`` for every registered overlay, or ``None`` when one will not read.

    An unreadable scope is not an absent one: dropping it can collapse a safe TIE
    into a single owner, and that owner then supplies the token and base URL the
    post is addressed with. So the whole attribution declines — the target reads
    as unowned — rather than the failed read silently picking a winner. Still never
    fatal: the failure is warned, not raised.
    """
    from teatree.core.overlay_loader import OverlayConfigResolver  # noqa: PLC0415 — deferred: keeps CLI startup light

    scopes: list[tuple[str, dict[str, list[str]]]] = []
    for name in OverlayConfigResolver.all_names():
        try:
            scopes.append((name, OverlayConfigResolver.owned_repos(name)))
        except Exception:
            logger.warning("Overlay %r owned_repos read failed while attributing a review target", name, exc_info=True)
            return None
    return scopes


def _owning_overlay(repo: str) -> "OverlayBase":
    """The overlay that owns *repo* — the single source of a review post's forge target.

    :func:`owning_overlay_name` matches the slug against each registered overlay's
    declared repos and then its declared namespace, so resolution stays total on a
    multi-overlay install. An unowned or ambiguously-owned slug still falls through
    to the ambient default, which fails loud naming the installed overlays rather
    than picking one.
    """
    from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415 — deferred: keeps CLI startup light

    return get_overlay(owning_overlay_name(repo) or None)


def _glab_login_token() -> str:
    """The token of the local ``glab`` login, or ``""`` when there is none.

    An absent ``glab`` binary is "no local login", not a read failure — this is the
    last-resort source, consulted only after the owning overlay and the explicit env
    value have both come up empty.
    """
    try:
        result = run_allowed_to_fail(["glab", "auth", "status", "-t"], expected_codes=None)
    except FileNotFoundError:
        return ""
    for line in result.stderr.splitlines():
        if "Token" in line and ":" in line:
            token_value = line.rsplit(":", 1)[-1].strip()
            if token_value:
                return token_value
    return ""


def read_token(repo: str) -> ReadOutcome[str]:
    """The API token for the forge that owns *repo*, with a failed read kept distinct.

    Resolution order: the owning overlay's configured token, then an explicitly-set
    ``$GITLAB_TOKEN``, then the local ``glab`` login. The overlay wins because it is the
    only source keyed to the target — one process-wide env value cannot be the right
    credential for two overlays' forges.
    """

    def _overlay_token() -> str:
        return _owning_overlay(repo).config.get_gitlab_token()

    outcome = guarded_read(_CRED_READ, _overlay_token, neutral="")
    if outcome.value:
        return outcome
    fallback = os.environ.get("GITLAB_TOKEN", "") or _glab_login_token()
    return ReadOutcome(value=fallback, failed=False) if fallback else outcome


def resolve_base_url(repo: str) -> str:
    """The GitLab API base URL a post to *repo* is addressed to — overlay first, then env.

    REFUSES rather than guessing (#3509): a silent fallback could redirect an outbound
    review post to a DIFFERENT GitLab instance. An explicitly-set ``$GITLAB_URL`` is
    still honoured — that is an operator's stated choice, not a guess — but with nothing
    to fall back to the read raises
    :class:`~teatree.cli.review.guarded_read.ReadRefusedError`.
    """

    def _overlay_url() -> str:
        return _owning_overlay(repo).config.gitlab_url

    env_url = os.environ.get("GITLAB_URL", "").strip()
    if not env_url:
        return read_or_refuse(_URL_READ, _overlay_url)
    return guarded_read(_URL_READ, _overlay_url, neutral=env_url).value or env_url
