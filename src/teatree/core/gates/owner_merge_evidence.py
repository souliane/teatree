"""Forge-bound owner-merge evidence for the delivered-time rubric gate."""

import logging
from typing import TYPE_CHECKING

from teatree.config import get_effective_settings
from teatree.core.gates.merge_evidence_gate import has_merge_audit_evidence, recorded_forge_merge_ref
from teatree.core.merge.ci_rollup import CodeHostQuery
from teatree.core.models import PullRequest

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket

logger = logging.getLogger(__name__)


def record_forge_merger(ticket: "Ticket") -> None:
    if has_merge_audit_evidence(ticket):
        return
    ref = recorded_forge_merge_ref(ticket)
    if ref is None:
        return
    recorded = ticket.extra["forge_merge_evidence"]
    existing = recorded.get("merged_by")
    if isinstance(existing, str) and existing.strip():
        return
    try:
        state = CodeHostQuery.for_ref(ref).pr_merge_state()
        sha = state.merge_commit_oid.strip()
        login = state.merged_by.strip() if isinstance(state.merged_by, str) else ""
        merged = state.is_merged
    except Exception:
        logger.warning("Could not read forge merger for ticket %s", ticket.pk, exc_info=True)
        return
    if not merged or sha.casefold() != str(recorded["merged_sha"]).casefold() or not login:
        return
    ticket.merge_extra(merge_into_dicts={"forge_merge_evidence": {"merged_by": login}})


def owner_merger_login(ticket: "Ticket") -> str | None:
    recorded = (ticket.extra or {}).get("forge_merge_evidence")
    if not isinstance(recorded, dict):
        return None
    login = recorded.get("merged_by")
    if not isinstance(login, str) or not (login := login.strip()):
        return None
    if has_merge_audit_evidence(ticket) or recorded_forge_merge_ref(ticket) is None:
        return None
    if PullRequest.objects.filter(ticket=ticket).live().exists():
        return None
    aliases = get_effective_settings(ticket.overlay or None).user_identity_aliases
    normalized = {alias.strip().casefold() for alias in aliases if isinstance(alias, str) and alias.strip()}
    return login if login.casefold() in normalized else None
