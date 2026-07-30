"""Host-facing MR/PR test-plan poster (F3.1).

The MR/PR-surface twin of the ticket/issue poster in :mod:`.post`. Splitting the
two surfaces keeps each module within the module-health LOC cap while both share
the same gates (on-behalf peek, upload existence check, blocked-body + forge-write
seam, hidden-marker idempotency) via helpers imported from :mod:`.post`.

The note is posted on the **MR/PR**; the ticket/issue poster lives in :mod:`.post`.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.evidence.test_plan_blocked_gate import check_blocked_body_from_config
from teatree.core.management.commands._test_plan.post import _comment_id, _verified_embed, find_existing_note
from teatree.core.management.commands._test_plan.render import test_plan_marker
from teatree.core.on_behalf_gate_recorded import (
    OnBehalfPostBlockedError,
    on_behalf_block_message,
    require_on_behalf_approval,
)
from teatree.core.on_behalf_post_receipt import notify_user_on_behalf_post
from teatree.core.send_proxy import route_forge_write
from teatree.types import RawAPIDict

__all__ = ["MrTestPlanPost", "post_mr_test_plan_comment"]

# The wire action key for the MR/PR-surface poster. It stays ``post_evidence``
# (NOT renamed to ``post_test_plan``): it is the PERSISTED value on live
# ``OnBehalfApproval`` rows and in ``on_behalf_post:<target>:post_evidence``
# BotPing idempotency keys, so the concept rename is user-facing only.
_MR_ON_BEHALF_ACTION = "post_evidence"


@dataclass(frozen=True, slots=True)
class MrTestPlanPost:
    """The validated CLI inputs for :func:`post_mr_test_plan_comment`.

    Bundles the flat ``pr post-test-plan`` flags into one value so the poster and
    its create-or-update helper stay within the argument-count gate. ``target``
    is the on-behalf / forge-write identity (``<repo>!<mr_iid>``) and doubles as
    the note's hidden idempotency-marker id, so an in-place update is scoped to
    THIS MR.
    """

    repo: str
    mr_iid: int
    title: str = "Test Plan"
    body: str = ""
    files: list[str] = field(default_factory=list)

    @property
    def target(self) -> str:
        return f"{self.repo}!{self.mr_iid}"


def _render_mr_note_body(*, title: str, body: str, embeds: list[str], marker_id: str) -> str:
    """Assemble the PR/MR test-plan comment body with the hidden idempotency marker.

    The marker (``<!-- t3-e2e-evidence ticket=<marker_id> -->``) makes the note
    findable for an idempotent in-place update scoped to THIS MR — replacing the
    former naive ``"## Test Plan" in body`` scan that could match (and clobber) a
    colleague's unrelated comment.
    """
    note_body = f"## {title}\n\n{body}" if body else f"## {title}\n\n_No details provided._"
    if embeds:
        note_body += "\n\n" + "\n\n".join(embeds)
    return f"{note_body}\n\n{test_plan_marker(ticket_id=marker_id)}"


def _create_or_update_pr_note(
    host: CodeHostBackend,
    *,
    post: MrTestPlanPost,
    match_id: int | None,
    body: str,
    write_out: Callable[[str], None],
) -> tuple[RawAPIDict, str, int]:
    """Create a new PR/MR comment or update the existing note through the on-behalf gate.

    The MR-surface twin of ``_create_or_update_note`` (which posts on the
    issue). Returns ``(publish_result, action, comment_id)``. The consume happens
    atomically with the post (#1879): a failed post burns no approval.
    """
    if match_id is not None:
        write_out(f"  Updating existing note {match_id}")
        result = require_on_behalf_approval(
            target=post.target,
            action=_MR_ON_BEHALF_ACTION,
            publish=lambda: host.update_pr_comment(repo=post.repo, pr_iid=post.mr_iid, comment_id=match_id, body=body),
        )
        return result, "updated", match_id
    result = require_on_behalf_approval(
        target=post.target,
        action=_MR_ON_BEHALF_ACTION,
        publish=lambda: host.post_pr_comment(repo=post.repo, pr_iid=post.mr_iid, body=body),
    )
    return result, "created", _comment_id(result)


def post_mr_test_plan_comment(
    host: CodeHostBackend, post: MrTestPlanPost, *, write_out: Callable[[str], None]
) -> RawAPIDict:
    """Gate + upload + create-or-update the one test-plan note on a PR/MR.

    The MR-surface twin of :func:`teatree.core.management.commands._test_plan.post.post_test_plan_comment`
    (which posts on the ticket/issue). Both posters now share the SAME gates so
    they can never drift (F3.1):

    * the non-consuming on-behalf peek fires FIRST — before any host call — so a
        blocked post touches no upload/list/comment API;
    * every uploaded artifact passes the #2156 :meth:`verify_upload` existence
        check (``_verified_embed``), never a blind ``upload["markdown"]`` that
        could reference a broken upload;
    * the assembled body is run through :func:`check_blocked_body_from_config`
        and the scanned forge-write seam (public-repo leak gate + #117 send-proxy);
    * the existing note is matched by THIS MR's hidden idempotency marker (via
        :func:`find_existing_note`), never a naive ``"## Test Plan" in body`` scan
        that could clobber a colleague's comment.

    The overlay's CI project path is a bare slug, so ``forge`` is left
    unqualified in the forge-write call: the leak gate then fails CLOSED (scans)
    on an unknown host rather than skipping the scan.
    """
    target = post.target
    # Peek (non-consuming) so an unapproved post refuses BEFORE any upload or
    # other host side effect; the consume happens atomically with the post below.
    if on_behalf_block_message(target, _MR_ON_BEHALF_ACTION):
        raise OnBehalfPostBlockedError(target, _MR_ON_BEHALF_ACTION)

    embeds: list[str] = []
    for filepath in post.files:
        embeds.append(_verified_embed(host, repo=post.repo, filepath=filepath, label=Path(filepath).name))
        write_out(f"  Uploaded: {filepath}")

    note_body = _render_mr_note_body(title=post.title, body=post.body, embeds=embeds, marker_id=target)
    check_blocked_body_from_config(note_body, target)
    # The shared forge-write seam (public-repo leak gate + #117 send-proxy) — same seam the MCP tools use.
    note_body = route_forge_write(forge="", repo=post.repo, text=note_body, action=_MR_ON_BEHALF_ACTION, target=target)

    existing = find_existing_note(host.list_pr_comments(repo=post.repo, pr_iid=post.mr_iid), ticket_id=target)
    match_id = existing.comment_id if existing else None
    result, _action, _comment = _create_or_update_pr_note(
        host, post=post, match_id=match_id, body=note_body, write_out=write_out
    )

    notify_user_on_behalf_post(
        target=target,
        action=_MR_ON_BEHALF_ACTION,
        destination=target,
        artifact_url=str(result.get("web_url") or result.get("html_url") or target),
        summary=f"{post.title} on {target}",
    )
    return result
