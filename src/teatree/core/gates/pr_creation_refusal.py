"""What ``ensure-pr`` checks on a PR spec before the forge create, and the rubric row a RULES.md edit owes."""

import logging

from teatree.core.backend_protocols import PullRequestSpec
from teatree.core.gates.privacy_gate import refusal_for_pr_generic_findings
from teatree.core.models import Rubric, Ticket
from teatree.core.send_proxy import forge_from_url
from teatree.quality.rule_clauses import RUBRIC_ROW, RulesDiff, rules_diff
from teatree.utils import git

logger = logging.getLogger(__name__)


def _rules_diff(spec: PullRequestSpec) -> RulesDiff | None:
    try:
        target = spec.target_branch or git.default_branch(spec.repo)
        return rules_diff(spec.repo, base=f"origin/{target}", head=spec.branch)
    except (RuntimeError, OSError):
        logger.warning("could not read the RULES.md diff of %s — the clause check did not run", spec.branch)
        return None


def pr_creation_refusal(spec: PullRequestSpec, *, remote: str) -> str | None:
    generic = refusal_for_pr_generic_findings(
        title=spec.title, description=spec.description, target_repo=remote, forge=forge_from_url(remote)
    )
    if generic:
        return generic
    diff = _rules_diff(spec)
    return diff.refusal() if diff is not None else None


def seed_rules_rubric_row(ticket: Ticket | None, spec: PullRequestSpec) -> None:
    if ticket is None:
        return
    diff = _rules_diff(spec)
    if diff is not None and diff.touched:
        Rubric.add_criteria(ticket, [RUBRIC_ROW])
