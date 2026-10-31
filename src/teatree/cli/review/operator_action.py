"""``t3 review approve`` / ``unapprove``: the one route, through a consenting operator's forge grant."""

from teatree.cli.review.github_operator_approval import publish_github_approval
from teatree.cli.review.operator_approval import publish_operator_approval
from teatree.cli.review.operator_target import resolve_operator
from teatree.cli.review.service import ReviewService
from teatree.core.merge.errors import MergePreconditionError
from teatree.core.merge.host_kind import forge_for_repo_slug
from teatree.utils.forge import normalize_forge


def _forge(repo: str, flag: str) -> str:
    if not flag:
        return forge_for_repo_slug(repo) or "gitlab"
    if forge := normalize_forge(flag):
        return forge
    message = f"unknown forge {flag!r}; valid: github, gitlab"
    raise ValueError(message)


def run_operator_action(repo: str, number: int, action: str, forge: str) -> tuple[str, int]:
    try:
        resolved = _forge(repo, forge)
        approver, token = resolve_operator(repo, number, action, resolved)
    except (ValueError, PermissionError, MergePreconditionError) as error:
        return str(error), 1
    if resolved == "github":
        return publish_github_approval(token, repo, number, action, approver)
    return publish_operator_approval(ReviewService(token, repo=repo, oauth=True), repo, number, action, approver)
