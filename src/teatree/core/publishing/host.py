"""The code-host methods behind each publish kind."""

from urllib.parse import urlparse

from teatree.core.backend_factory import code_host_from_overlay
from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.issue_writes.issue_candidate_selection import description_of
from teatree.core.overlays.repo_ownership import owning_overlay_for_repo
from teatree.core.publishing.publish import PublishKind, PublishRefusedError
from teatree.core.send_proxy import forge_from_url
from teatree.types import RawAPIDict
from teatree.utils.url_slug import pr_ref_from_url, slug_from_issue_or_pr_url


def build_publish_host(target: str) -> "CodeHostPublishHost":
    repo = slug_from_issue_or_pr_url(urlparse(target).path)
    overlay = owning_overlay_for_repo(repo, forge=forge_from_url(target)) if repo else ""
    backend = code_host_from_overlay(overlay or None)
    if backend is None:
        msg = f"no code host is configured for {target}"
        raise PublishRefusedError(msg)
    return CodeHostPublishHost(backend)


class CodeHostPublishHost:
    def __init__(self, backend: CodeHostBackend) -> None:
        self._backend = backend

    def write(self, kind: PublishKind, target: str, text: str) -> str:
        issue_url = _github_issue_url(target)
        if kind is PublishKind.COMMENT:
            result = self._backend.post_issue_comment(issue_url=issue_url, body=text)
        else:
            result = self._backend.update_issue(issue_url=issue_url, body=text)
        if error := result.get("error"):
            msg = f"the forge refused the {kind.value} write to {target}: {error}"
            raise PublishRefusedError(msg)
        return str(result.get("id", ""))

    def read_back(self, kind: PublishKind, target: str, handle: str) -> str | None:
        issue_url = _github_issue_url(target)
        if kind is PublishKind.COMMENT:
            return _comment_body(self._backend.list_issue_comments(issue_url=issue_url), handle)
        issue = self._backend.get_issue(issue_url)
        return None if issue.get("error") else description_of(issue)


def _github_issue_url(target: str) -> str:
    """GitHub serves a PR through its issue endpoints; an issue URL passes through."""
    pr_ref = pr_ref_from_url(target)
    if pr_ref is None:
        return target
    if pr_ref.host_kind != "github":
        msg = f"publish writes a pull request on GitHub only; {target} is a {pr_ref.host_kind} merge request"
        raise PublishRefusedError(msg)
    return f"https://github.com/{pr_ref.slug}/issues/{pr_ref.pr_id}"


def _comment_body(comments: list[RawAPIDict], handle: str) -> str | None:
    body = next((c.get("body") for c in comments if str(c.get("id")) == handle), None)
    return body if isinstance(body, str) else None
