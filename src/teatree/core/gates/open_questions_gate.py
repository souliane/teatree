"""Warn-only Open-questions-section gate on PR creation (souliane/teatree#1933).

Doctrine lives in ``skills/ship/SKILL.md`` § "Open Questions & Assumptions":
any open question (solved or not) and any assumption not 100% explicit from the
spec must be listed in the commit message body AND the PR description under an
"Open questions & assumptions" section. This module is the smallest
deterministic enforcement artifact for the PR side: when a PR body lacks the
section heading, it WARNS and never hard-fails — the heuristic (a body could
legitimately carry the section under a slightly different heading) is not
reliable enough to block, so the gate warns per the repo doctrine that a gate
without a reliable heuristic warns.

A second warn covers the ``decided-by-user`` STATUS the section's entries carry
(souliane/teatree#4371): an entry citing a ``PlanArtifact`` the maker/loop side
recorded — or no record at all — presents an agent decision as owner ratification,
and a later reader auditing why the shipped change contradicts its issue trusts
the attribution and never re-derives the evidence. The citation is checked, never
the prose, so an entry citing nothing is silent.

Shared by both PR-creation chokepoints (``ShipExecutor._build_pr_spec`` and the
orphan-branch ``create_or_defer_pr``) so the warn cannot drift between them.
"""

import logging
import re

from django.core.exceptions import ImproperlyConfigured
from django.db import DatabaseError

logger = logging.getLogger(__name__)

OPEN_QUESTIONS_HINT = "add an 'Open questions & assumptions' section"
RATIFICATION_HINT = "a 'decided-by-user' status must cite a record the OWNER made"

_SECTION_HEADING_RE = re.compile(
    r"^\s*(?:#{1,6}\s+)?open\s+questions(?:\s*(?:&|and)\s*assumptions)?\s*:?\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_RATIFICATION_STATUS_RE = re.compile(r"decided[\s_-]*by[\s_-]*user", re.IGNORECASE)
_PLAN_ARTIFACT_CITATION_RE = re.compile(r"plan[\s_-]*artifact\s*#?\s*(\d+)", re.IGNORECASE)


def has_open_questions_section(body: str) -> bool:
    return bool(_SECTION_HEADING_RE.search(body or ""))


def cited_ratification_artifacts(body: str) -> tuple[int, ...]:
    """The ``PlanArtifact`` pks cited on a ``decided-by-user`` line, first-seen order."""
    cited: dict[int, None] = {}
    for line in (body or "").splitlines():
        if _RATIFICATION_STATUS_RE.search(line):
            for match in _PLAN_ARTIFACT_CITATION_RE.finditer(line):
                cited.setdefault(int(match.group(1)), None)
    return tuple(cited)


def warn_if_open_questions_missing(body: str) -> str | None:
    if has_open_questions_section(body):
        return None
    message = (
        f"PR body has no 'Open questions' section heading — {OPEN_QUESTIONS_HINT} "
        "listing each open question / non-explicit assumption "
        "(status: decided-by-user / assumed / open). See skills/ship § "
        "'Open Questions & Assumptions'."
    )
    logger.warning(message)
    return message


def warn_if_owner_ratification_unbacked(body: str) -> str | None:
    cited = cited_ratification_artifacts(body)
    if not cited:
        return None
    try:
        unbacked = _citations_the_owner_did_not_make(cited)
    except (DatabaseError, ImproperlyConfigured):
        logger.debug("ratification citations unread — the plan-artifact store is unreachable", exc_info=True)
        return None
    if not unbacked:
        return None
    message = (
        f"PR body claims 'decided-by-user' citing {', '.join(unbacked)} — {RATIFICATION_HINT}. "
        "Mark the entry 'assumed', or cite the record that carries the owner's decision, so a "
        "later reader does not take an agent decision for ratified scope."
    )
    logger.warning(message)
    return message


def _citations_the_owner_did_not_make(pks: tuple[int, ...]) -> list[str]:
    """The cited artifacts that back no owner decision, rendered with why."""
    from teatree.core.models import PlanArtifact  # noqa: PLC0415 — deferred import (cycle-safe / pre-app-registry)
    from teatree.core.models.reviewer_identity import (  # noqa: PLC0415 — deferred import (cycle-safe)
        is_non_reviewer_role,
    )

    recorded = dict(PlanArtifact.objects.filter(pk__in=pks).values_list("pk", "recorded_by"))
    return [
        f"PlanArtifact {pk} (no such record)"
        if pk not in recorded
        else f"PlanArtifact {pk} (recorded_by={recorded[pk]!r})"
        for pk in pks
        if pk not in recorded or is_non_reviewer_role(recorded[pk])
    ]
