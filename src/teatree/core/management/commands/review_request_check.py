"""``t3 review-request check`` — race-safe pre-post dedup gate (#1084).

Backs the SKILL.md / slack.md mandate: the agent runs this in the SAME
turn as a review-request post and aborts on SUPPRESS. It reads the live
review channel with the same token the post would use and takes the
atomic DB claim — so a duplicate (agent re-post, or a user's manual
out-of-band post) is impossible.
"""

from typing import Annotated

import typer
from django_typer.management import TyperCommand, command

from teatree.core.review_request_guard import resolve_guard_target, should_post_review_request
from teatree.types import RawAPIDict


class Command(TyperCommand):
    @command()
    def handle(
        self,
        mr_url: Annotated[str, typer.Option("--mr-url", help="Canonical MR/PR URL to dedup.")],
    ) -> RawAPIDict:
        """Decide POST or SUPPRESS for a review-request message.

        Exit/output is machine-readable: ``action`` is ``post`` or
        ``suppress``; ``permalink`` points at the existing message when
        suppressed by a live-channel match. The caller MUST abort the
        post on ``suppress``.
        """
        target = resolve_guard_target()
        if target is None:
            return {
                "action": "suppress",
                "reason": "no_review_channel_or_token",
                "mr_url": mr_url,
            }

        decision = should_post_review_request(mr_url=mr_url, target=target)
        return {
            "action": decision.action,
            "reason": decision.reason,
            "permalink": decision.permalink,
            "author": decision.author,
            "mr_url": mr_url,
        }
