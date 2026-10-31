"""GitHub approval publishing through the operator's GitHub App user token."""

from teatree.backends.github.client import GitHubCodeHost
from teatree.backends.github.pr_notes import GitHubPrNotes
from teatree.cli.review.operator_approval import (
    OperatorApprover,
    claim_publish_audit,
    identity_refusal,
    pending_consent,
)
from teatree.core.modelkit.review_state import ReviewState
from teatree.core.models.operator_identity import identity_key
from teatree.utils.run import CommandFailedError


def _approve_and_verify(host: GitHubCodeHost, notes: GitHubPrNotes, repo: str, pr: int, login: str) -> tuple[str, int]:
    try:
        notes.approve(repo=repo, pr_iid=pr)
        state = host.get_review_state(pr_url=f"https://github.com/{repo}/pull/{pr}", reviewer=login)
    except CommandFailedError as error:
        return f"Failed: {error.stderr.strip() or error}", 1
    if state is not ReviewState.APPROVED:
        return f"GitHub did not record the approval: {login}'s latest review on {repo}#{pr} is {state}, not APPROVED", 1
    return f"OK approved {repo}#{pr}", 0


def publish_github_approval(token: str, repo: str, pr: int, action: str, approver: OperatorApprover) -> tuple[str, int]:
    if action != "approve":
        return "GitHub offers no un-approve call; dismiss your review on the pull request page instead", 1
    approval = pending_consent(f"{repo}!{pr}", action, approver.consent_key)
    if approval is None:
        return f"No consent from {approver.consent_key} for {action} on {repo}!{pr}", 1
    host = GitHubCodeHost(token=token)
    try:
        login = host.current_user()
    except CommandFailedError as error:
        return f"Could not read the GitHub login behind the grant: {error.stderr.strip() or error}", 1
    if not login:
        return "Could not read the GitHub login behind the grant", 1
    if refusal := identity_refusal(approver, identity_key("github", login)):
        return refusal, 1
    return claim_publish_audit(approval, lambda: _approve_and_verify(host, GitHubPrNotes(token), repo, pr, login))
