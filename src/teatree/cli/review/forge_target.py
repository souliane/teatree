"""Where a review post is addressed, and with what credential.

``t3 review <cmd> <repo> <mr> …`` names its target in the invocation, so both
coordinates a post needs — the forge base URL and the API token — are derivable from
that slug: the overlay that owns the repo carries them. Resolving them from the
AMBIENT overlay instead makes the whole surface conditional on how many overlays
happen to be registered, because ``get_overlay()`` raises ``Multiple overlays found``
as soon as there is more than one and no explicit pin (souliane/teatree#3793).

Absent an explicit environment override, both reads go through the same owning overlay,
so a post can never be addressed to one forge with another's credential.

A read that FAILED is kept distinct from one that found nothing
(:class:`~teatree.cli.review.guarded_read.ReadOutcome`): the two need different
remediation, and reporting the first as the second sends the operator to a re-login
that changes nothing (souliane/teatree#3794).
"""

import os
from typing import TYPE_CHECKING

from teatree.cli.review.guarded_read import ReadOutcome, guarded_read, read_or_refuse
from teatree.config.credential_pass_key import PassKeySource

if TYPE_CHECKING:
    from teatree.core.overlay import OverlayBase

_CRED_READ = "the review API token from the overlay that owns the target repo"
_URL_READ = "the review GitLab base URL from the overlay config"

# ``t3 review`` posts only to GitLab (``send_routing.route_forge_send`` pins the
# same forge), so only GitLab-hosted namespace declarations may attribute a target.
_REVIEW_FORGE = "gitlab"


def owning_overlay_name(repo: str) -> str:
    """The overlay that owns *repo* on the review forge — ``""`` when it is not exactly one.

    The review-surface binding of
    :func:`~teatree.core.overlays.repo_ownership.owning_overlay_for_repo`, which the
    publication privacy gate also consults from below the CLI layer. ``t3 review``
    posts only to GitLab, so the forge is pinned here rather than taken per call.

    The namespace fallback is applied in that resolver rather than inside
    ``infer_overlay_for_url`` because ``owned_repos`` "gates ONLY the unknown-repo
    approval decision, never merge-without-review"
    (:class:`~teatree.core.overlay.OverlayConfig`) and that resolver also feeds merge
    authorization — so the namespace declaration reaches the surfaces it was added for
    and nothing else.
    """
    from teatree.core.overlays.repo_ownership import (  # noqa: PLC0415 — deferred: keeps CLI startup light
        owning_overlay_for_repo,
    )

    return owning_overlay_for_repo(repo, forge=_REVIEW_FORGE)


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


def read_token(repo: str) -> ReadOutcome[str]:
    """The API token for the forge that owns *repo*, with a failed read kept distinct.

    Resolution order: an explicitly-set ``$GITLAB_TOKEN``, then the owning overlay's
    configured token. A logged-in ``glab`` account is never inherited: it is ambient
    process state, not an explicit authorization for this review write.

    The overlay read is ``get_gitlab_token()``, the OWNER credential — deliberately NOT
    ``get_gitlab_token_for_remote()``. That override names the AUTHORING credential, and
    the forge bars an MR's author from approving it, so routing this surface through it
    would record every review and approval under the bot: the one identity that must
    never carry an approval. Reviewing and approving stay the owner's account, acted as
    by the agent. Pinned by
    ``tests/teatree_cli/review/test_forge_target.py::TestApprovalUsesTheOwnerNotTheAuthoringCredential``.
    """
    if explicit := os.environ.get("GITLAB_TOKEN", ""):
        return ReadOutcome(value=explicit, failed=False)

    def _overlay_token() -> str:
        config = _owning_overlay(repo).config
        token = config.get_gitlab_token()
        if token:
            return token
        resolution = config.resolve_pass_key("gitlab_token")
        if resolution.source is PassKeySource.UNREADABLE:
            msg = f"{resolution.setting} resolved from {resolution.source.value}; refusing ambient authentication"
            raise RuntimeError(msg)
        return ""

    return guarded_read(_CRED_READ, _overlay_token, neutral="")


def resolve_base_url(repo: str) -> str:
    """The GitLab API base URL a post to *repo* is addressed to — explicit env, then overlay.

    REFUSES rather than guessing (#3509): a silent fallback could redirect an outbound
    review post to a DIFFERENT GitLab instance. An explicitly-set ``$GITLAB_URL`` is
    still honoured — that is an operator's stated choice, not a guess — but with nothing
    to fall back to the read raises
    :class:`~teatree.cli.review.guarded_read.ReadRefusedError`.
    """

    def _overlay_url() -> str:
        return _owning_overlay(repo).config.gitlab_url

    if env_url := os.environ.get("GITLAB_URL", "").strip():
        return env_url
    return read_or_refuse(_URL_READ, _overlay_url)
