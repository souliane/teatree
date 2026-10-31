"""Seed a planless ticket's rubric from its issue's Acceptance section, reporting every way that fails."""

import logging
from typing import TYPE_CHECKING

from teatree.core.intake.acceptance_criteria import ACCEPTANCE_HEADING, acceptance_rows, classify_row, render_criterion
from teatree.core.models import Rubric
from teatree.core.models.plan_artifact import PlanArtifact
from teatree.core.models.rubric import RubricError

if TYPE_CHECKING:
    from teatree.core.backend_protocols import CodeHostBackend
    from teatree.core.models import Ticket

logger = logging.getLogger(__name__)

NOTICE_PREFIX = "Rubric not seeded:"


def _issue_body(host: "CodeHostBackend", issue_url: str) -> str | None:
    try:
        raw = host.get_issue(issue_url)
    except Exception:
        logger.exception("could not read %s to seed its rubric", issue_url)
        return None
    if raw.get("error"):
        logger.warning("could not read %s to seed its rubric: %s", issue_url, raw["error"])
        return None
    return str(raw.get("body") or raw.get("description") or "")


def _seed(ticket: "Ticket", body: str) -> str:
    if not ACCEPTANCE_HEADING.search(body):
        return "the issue has no Acceptance section"
    rows = acceptance_rows(body)
    if not rows:
        return "the issue's Acceptance section lists no bullets"
    try:
        Rubric.add_criteria(ticket, [render_criterion(row, classify_row(row)) for row in rows])
    except RubricError as exc:
        logger.warning("rubric seed for ticket %s refused: %s", ticket.pk, exc)
        return "its Acceptance rows were refused as unfalsifiable"
    return ""


def seed_rubric_notice(host: "CodeHostBackend", ticket: "Ticket | None", description: str) -> str:
    """*description*, with a notice appended when the rubric could not be seeded for a ticket with no plan."""
    if ticket is None or PlanArtifact.objects.filter(ticket=ticket).exists():
        return description
    body = _issue_body(host, ticket.issue_url)
    reason = "the issue could not be read" if body is None else _seed(ticket, body)
    return f"{description}\n\n{NOTICE_PREFIX} {reason}." if reason else description
