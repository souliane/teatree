"""Apply the overlay's standing reviewer policy to MRs that are already open.

``pr_auto_reviewers`` rides the MR-creation POST, so it can never reach an MR
opened before the policy landed. This is the catch-up pass, and it is narrow by
CONSTRUCTION rather than by convention — which is what keeps it from becoming
the general reviewer-assignment surface ``handle_block_self_reviewer_assign``
refuses. Neither side of the assignment can be named by a caller:

* the reviewers come from ``pr_auto_reviewers`` alone — there is no username
    parameter anywhere in the call chain, so no caller can aim it at a colleague;
* the only MRs in scope are those authored by the identity the factory itself
    acts as on the repo, which exists only where the overlay gave that repo its
    own credential precisely so its MRs are not the owner's. A repo written under
    the overlay-wide credential has no such identity, and the whole run refuses.

GitLab-shaped, like the creation-time half: ``pr_auto_reviewers`` is not applied
on the GitHub create path either, since ``gh pr create --reviewer`` is itself a
surface the gate refuses.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, cast, runtime_checkable

from teatree.core.runners.ship import overlay_pr_reviewers
from teatree.types import RawAPIDict
from teatree.utils.git_remote import slug_from_remote

if TYPE_CHECKING:
    from teatree.core.overlay import OverlayBase

type Outcome = Literal["assigned", "unchanged", "refused", "failed"]


class ReviewerPolicyError(RuntimeError):
    """The run cannot proceed — no policy to apply, or no bot identity to scope it to."""


@runtime_checkable
class ReviewerAssignable(Protocol):
    """The three host reads/writes the policy needs, declared where they are consumed.

    Structural rather than a widening of ``CodeHostBackend``: ``assign_reviewer``
    is GitLab-only, and a Protocol member every other host answers with a stub is
    a surface paid for at each implementation to serve one caller.
    """

    def current_user(self) -> str: ...

    def list_prs(self, *, repo: str, state: str = "", author: str = "") -> list[RawAPIDict]: ...

    def assign_reviewer(self, *, pr_url: str, username: str) -> bool: ...


@dataclass(frozen=True)
class ReviewerPolicyRow:
    url: str
    author: str
    outcome: Outcome
    detail: str

    def line(self) -> str:
        return f"{self.outcome.upper():<9} {self.url} (author {self.author}) — {self.detail}"


@dataclass(frozen=True)
class ReviewerPolicyReport:
    rows: list[ReviewerPolicyRow]
    reviewers: list[str]
    bot: str

    @property
    def failed(self) -> bool:
        return any(row.outcome == "failed" for row in self.rows)

    def lines(self) -> list[str]:
        return [row.line() for row in self.rows]


def _username(entry: object) -> str:
    if not isinstance(entry, dict):
        return ""
    name = cast("RawAPIDict", entry).get("username")
    return name if isinstance(name, str) else ""


def _reviewer_usernames(mr: RawAPIDict) -> set[str]:
    entries = mr.get("reviewers")
    return {_username(entry) for entry in entries} if isinstance(entries, list) else set()


def _factory_bot_identity(overlay: "OverlayBase", remote: str, host: ReviewerAssignable) -> str:
    """The distinct non-owner identity the factory acts as on *remote*, else ``""``.

    An overlay hands one repo its own credential exactly so the MRs there are
    authored by a non-human the owner stays eligible to approve. Where the
    credential IS the overlay-wide one, the factory acts as the owner and no MR
    on that repo can be in scope — the same scoping the creation-time half
    applies through ``pr_reviewers_for_remote``, asked of the one predicate.
    """
    if not overlay.config.acts_as_distinct_identity_on(remote):
        return ""
    return host.current_user()


def _apply_to_mr(
    host: ReviewerAssignable,
    mr: RawAPIDict,
    *,
    bot: str,
    reviewers: list[str],
    dry_run: bool,
) -> ReviewerPolicyRow:
    url = str(mr.get("web_url", ""))
    author = _username(mr.get("author"))
    if author != bot:
        return ReviewerPolicyRow(url, author, "refused", f"author is not the factory bot {bot}")

    present = _reviewer_usernames(mr)
    missing = [name for name in reviewers if name not in present]
    if not missing:
        return ReviewerPolicyRow(url, author, "unchanged", "already carries the policy reviewers")
    if dry_run:
        return ReviewerPolicyRow(url, author, "assigned", f"would assign {', '.join(missing)}")

    unassigned = [name for name in missing if not host.assign_reviewer(pr_url=url, username=name)]
    if unassigned:
        return ReviewerPolicyRow(url, author, "failed", f"could not assign {', '.join(unassigned)}")
    return ReviewerPolicyRow(url, author, "assigned", f"assigned {', '.join(missing)}")


def apply_reviewer_policy(
    overlay: "OverlayBase",
    host: ReviewerAssignable,
    *,
    remote: str,
    dry_run: bool = False,
) -> ReviewerPolicyReport:
    """Apply ``pr_auto_reviewers`` to every open bot-authored MR on *remote*'s repo.

    Idempotent: an MR already carrying the policy reviewers is reported unchanged,
    never re-written. Every MR is reported — a refused one loudly, so an
    out-of-scope MR is visible rather than silently skipped.
    """
    reviewers = overlay_pr_reviewers(overlay)
    if not reviewers:
        msg = "no pr_auto_reviewers configured for this overlay — there is no policy to apply"
        raise ReviewerPolicyError(msg)

    slug = slug_from_remote(remote)
    if not slug:
        msg = f"could not resolve a repo slug from the remote {remote!r}"
        raise ReviewerPolicyError(msg)

    bot = _factory_bot_identity(overlay, remote, host)
    if not bot:
        msg = (
            f"{slug} is written under the overlay's own credential, so its MRs are the "
            "owner's — the reviewer policy is applied only to bot-authored MRs"
        )
        raise ReviewerPolicyError(msg)

    rows = [
        _apply_to_mr(host, mr, bot=bot, reviewers=reviewers, dry_run=dry_run)
        for mr in host.list_prs(repo=slug, state="opened")
    ]
    return ReviewerPolicyReport(rows=rows, reviewers=reviewers, bot=bot)
