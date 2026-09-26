"""``t3 review-request check`` — race-safe pre-post dedup gate (#1084).

Backs the SKILL.md / slack.md mandate: the agent runs this in the SAME
turn as a review-request post and aborts on SUPPRESS. It reads the live
review channel with the same token the post would use — so a duplicate
(agent re-post, or a user's manual out-of-band post) is detected. It is
strictly decision-only: it takes NO durable ``ReviewRequestPost`` claim
(``peek_should_post_review_request``), so it can never leave an orphan
that wedges a later real post on ``already_claimed`` (#1103).

A review-exempt repo is refused ahead of everything else, and is the ONE refusal
here that exits non-zero: it is permanent and repo-level, so no later state turns
it into a post and an unattended caller should branch on it rather than re-run.
Every other verdict stays exit 0 because it decides only THIS attempt.
"""

import json
import logging
from typing import Annotated, NoReturn

import typer
from django_typer.management import TyperCommand, command

from teatree.config import get_effective_settings
from teatree.core.backend_factory import code_host_from_overlay
from teatree.core.gates.review_request_batch_gate import refusal_payload, work_group_batch_refusal
from teatree.core.gates.review_request_draft_gate import draft_refusal_reason
from teatree.core.gates.review_request_guard import (
    overlay_for_mr_url,
    peek_should_post_review_request,
    resolve_guard_target,
)
from teatree.core.review.repo_exemption import mr_url_is_review_exempt
from teatree.core.review.review_candidate import _is_self_authored
from teatree.types import RawAPIDict

logger = logging.getLogger(__name__)


def _owner_authorship(mr_url: str, overlay_name: str) -> bool | None:
    """Resolve fresh forge authorship against the configured owner aliases."""
    try:
        host = code_host_from_overlay(overlay_name or None)
        identities = tuple(get_effective_settings(overlay_name or None).user_identity_aliases)
    except Exception:
        logger.exception("review_request_check: could not resolve owner identity dependencies for %s", mr_url)
        return None
    return _is_self_authored(mr_url, host, identities)


class Command(TyperCommand):
    @command()
    def handle(
        self,
        mr_url: Annotated[str, typer.Option("--mr-url", help="Canonical MR/PR URL to dedup.")],
    ) -> RawAPIDict:
        """Decide POST or SUPPRESS for a review-request message.

        Exit/output is machine-readable: ``action`` is ``post``, ``suppress``, or
        ``refused``; ``permalink`` points at the existing message when suppressed
        by a live-channel match. The caller MUST abort the post on ``suppress``
        and on ``refused`` — ``review_exempt_repo`` (exit 2), ``draft_mr`` (the MR
        is held back as a Draft), ``draft_state_unknown`` (the forge could not be
        read, so a Draft cannot be ruled out) and ``work_group_not_ready`` (a
        sibling merge request in the same unit of work is not review-ready, with
        the per-member detail in ``blockers``) are all refusals.
        """
        overlay_name = overlay_for_mr_url(mr_url)
        if mr_url_is_review_exempt(mr_url, overlay_name=overlay_name):
            self._refuse_review_exempt(mr_url)

        draft_refusal = draft_refusal_reason(mr_url, overlay_name=overlay_name)
        if draft_refusal:
            return {"action": "refused", "reason": draft_refusal, "mr_url": mr_url}

        batch_refusal = work_group_batch_refusal(mr_url, overlay_name=overlay_name)
        if batch_refusal is not None:
            return refusal_payload(batch_refusal, mr_url=mr_url)

        authorship = _owner_authorship(mr_url, overlay_name)
        if authorship is not True:
            return {
                "action": "refused",
                "reason": "foreign_author" if authorship is False else "authorship_unreadable",
                "mr_url": mr_url,
            }

        target = resolve_guard_target(overlay_name=overlay_name)
        if target is None:
            return {
                "action": "suppress",
                "reason": "no_review_channel_or_token",
                "mr_url": mr_url,
            }

        decision = peek_should_post_review_request(mr_url=mr_url, target=target, overlay=overlay_name)
        return {
            "action": decision.action,
            "reason": decision.reason,
            "permalink": decision.permalink,
            "author": decision.author,
            "mr_url": mr_url,
        }

    def _refuse_review_exempt(self, mr_url: str) -> NoReturn:
        """Print the refusal dict and exit 2 — the permanent, repo-level verdict.

        Raised before the draft probe and the channel resolve, so an exempt repo
        costs no forge or Slack round-trip. ``SystemExit`` rather than a returned
        dict because a ``typer.Exit`` under ``call_command`` exits 0, which would
        report a refusal as a success to the caller branching on the code.
        """
        self.stdout.write(json.dumps({"action": "refused", "reason": "review_exempt_repo", "mr_url": mr_url}))
        raise SystemExit(2)
