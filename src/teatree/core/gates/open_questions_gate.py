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

Shared by both PR-creation chokepoints (``ShipExecutor._build_pr_spec`` and the
orphan-branch ``create_or_defer_pr``) so the warn cannot drift between them.
"""

import logging
import re

logger = logging.getLogger(__name__)

OPEN_QUESTIONS_HINT = "add an 'Open questions & assumptions' section"

_SECTION_HEADING_RE = re.compile(
    r"^\s*(?:#{1,6}\s+)?open\s+questions(?:\s*(?:&|and)\s*assumptions)?\s*:?\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def has_open_questions_section(body: str) -> bool:
    return bool(_SECTION_HEADING_RE.search(body or ""))


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
