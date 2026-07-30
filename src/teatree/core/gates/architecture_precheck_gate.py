"""Warn-only architecture pre-check gate on PR creation.

Doctrine lives in ``skills/architecture-design/SKILL.md``: an architecture-
touching change carries a ``## Architecture pre-check`` section in the PR body,
and every required check — the removability / harness-vs-data check (#10)
included — must carry a real answer. This module is the smallest deterministic
enforcement artifact for the PR side: it runs the
``teatree.quality.architecture_precheck`` validator against the PR body and,
when the body carries a pre-check section that leaves a required check
unanswered, it WARNS. It never hard-fails — whether a given PR needs the section
at all is not a reliable enough signal to block, so the gate warns per the repo
doctrine that a gate without a reliable heuristic warns (mirrors
``open_questions_gate``).

A body with no pre-check section — a tactical change that legitimately skips the
gate — is silent, so ``precheck_findings``'s all-unanswered result on freeform
prose never surfaces as a spurious warn.

Shared by both PR-creation chokepoints (``ShipExecutor._build_pr_spec`` and the
orphan-branch ``create_or_defer_pr``) so the warn cannot drift between them.
"""

import logging
import re

from teatree.quality.architecture_precheck import precheck_findings

logger = logging.getLogger(__name__)

ARCHITECTURE_PRECHECK_HINT = "fill every architecture pre-check answer"

_PRECHECK_HEADING_RE = re.compile(
    r"^\s*#{1,6}\s+architecture\s+pre-?check\b",
    re.IGNORECASE | re.MULTILINE,
)


def has_precheck_section(body: str) -> bool:
    """True when *body* carries an ``## Architecture pre-check`` heading."""
    return bool(_PRECHECK_HEADING_RE.search(body or ""))


def warn_if_precheck_incomplete(body: str) -> str | None:
    """Warn when a PR body's architecture pre-check leaves a required check unanswered.

    Only a body that carries a pre-check section is validated; a PR without one
    is silent. When the section is present, every unanswered required check
    (removability #10 included) is surfaced. Returns the warning message (also
    logged) or ``None`` when nothing is missing.
    """
    if not has_precheck_section(body):
        return None
    findings = precheck_findings(body or "")
    if not findings:
        return None
    message = (
        f"PR body carries an 'Architecture pre-check' section but leaves "
        f"{len(findings)} required check(s) unanswered: {'; '.join(findings)}. "
        f"{ARCHITECTURE_PRECHECK_HINT} — see skills/architecture-design § 'The ten checks'."
    )
    logger.warning(message)
    return message
