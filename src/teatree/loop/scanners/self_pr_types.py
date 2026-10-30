"""Forge-neutral summary of a self-authored open PR and the port that lists them."""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from teatree.core.modelkit.forge_readability import CHECKS_FAILED

#: The required-checks verdicts that say the head has not settled: the author is
#: about to replace it, so a review dispatched now lands on a tree nobody merges.
UNSETTLED_CHECK_VERDICTS = frozenset({"pending", CHECKS_FAILED})


@dataclass(frozen=True, slots=True)
class PrSummary:
    """Decoded subset of a PR's ``gh`` payload, plus the head's required-checks standing.

    ``checks_unsettled`` is the one field the ``gh`` payload does not carry: the
    adapter stamps it from :func:`head_checks_unsettled` so both self-PR scanners
    read one already-resolved answer instead of each probing the forge.
    """

    slug: str
    number: int
    head_sha: str
    is_draft: bool
    changed_files: tuple[str, ...]
    url: str = ""
    title: str = ""
    author: str = ""
    checks_unsettled: bool = False


@runtime_checkable
class SelfPrApi(Protocol):
    """Adapter listing self-authored open PRs — mockable in tests."""

    def list_open_self_prs(self, *, slug: str) -> list[PrSummary]: ...  # pragma: no branch
