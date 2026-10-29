import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from teatree.core.backend_registry import get_backend_provider
from teatree.core.merge import CodeHostQuery
from teatree.core.merge.host_kind import forge_for_repo_slug
from teatree.loop.scanners.codex_review import UNSETTLED_CHECK_VERDICTS, CodexPrApi, PrSummary
from teatree.types import RawAPIDict, ScannerError, ScannerErrorClass
from teatree.utils.pr_ref import PrRef

if TYPE_CHECKING:
    from teatree.core.backend_protocols import CodeHostBackend

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class GlabCodexPrApi:
    token: str = ""

    def _backend(self) -> "CodeHostBackend":
        return get_backend_provider().build_gitlab_host(token=self.token, base_url="")

    def list_open_self_prs(self, *, slug: str) -> list[PrSummary]:
        host = self._backend()
        try:
            return self._read(host, slug=slug)
        except ScannerError:
            raise
        except Exception as exc:
            raise ScannerError(
                scanner="codex_review",
                error_class=ScannerErrorClass.UNKNOWN,
                detail=f"GitLab self-PR read failed for {slug!r}: {type(exc).__name__}: {exc}",
            ) from exc

    def _read(self, host: "CodeHostBackend", *, slug: str) -> list[PrSummary]:
        repo = host.get_repo(repo=slug)
        if repo.get("error"):
            message = f"GitLab project {slug!r} is unreadable: {repo['error']}"
            raise ValueError(message)
        author = host.current_user().strip()
        if not author:
            message = f"GitLab current user is unreadable for {slug!r}"
            raise ValueError(message)
        raw_mrs = host.list_prs(repo=slug, state="open", author=author)
        summaries: list[PrSummary] = []
        for raw in raw_mrs:
            if _author(raw) != author.casefold():
                continue
            summary = self._summary(host, slug=slug, author=author, raw=raw)
            if summary is not None:
                summaries.append(summary)
        return summaries

    @staticmethod
    def _summary(host: "CodeHostBackend", *, slug: str, author: str, raw: RawAPIDict) -> PrSummary | None:
        iid = raw.get("iid")
        head = raw.get("sha")
        url = raw.get("web_url")
        if not isinstance(iid, int) or not isinstance(head, str) or not head or not isinstance(url, str):
            message = f"GitLab MR list returned an incomplete entry for {slug!r}"
            raise ValueError(message)
        diff = host.get_pr_diff(repo=slug, pr_iid=iid)
        paths = tuple(
            dict.fromkeys(
                path
                for entry in diff
                if isinstance(entry, dict)
                if isinstance(path := entry.get("new_path") or entry.get("old_path"), str) and path
            )
        )
        if not paths:
            logger.warning("GitLab MR !%s in %s has no readable changed paths; skipping review", iid, slug)
            return None
        draft = raw.get("draft") is True
        unsettled = False
        if not draft:
            ref = PrRef(slug=slug, pr_id=iid, host_kind="gitlab")
            unsettled = CodeHostQuery(ref=ref, backend=host).required_checks_status() in UNSETTLED_CHECK_VERDICTS
        return PrSummary(
            slug=slug,
            number=iid,
            head_sha=head,
            is_draft=draft,
            changed_files=paths,
            url=url,
            title=str(raw.get("title") or ""),
            author=author.casefold(),
            checks_unsettled=unsettled,
        )


def _author(raw: object) -> str:
    if not isinstance(raw, dict):
        message = "GitLab MR list returned a non-object entry"
        raise TypeError(message)
    value = raw.get("author")
    if not isinstance(value, dict):
        message = "GitLab MR list returned an entry without an author"
        raise TypeError(message)
    login = value.get("username")
    if not isinstance(login, str) or not login:
        message = "GitLab MR list returned an entry without an author username"
        raise ValueError(message)
    return login.casefold()


@dataclass(slots=True)
class ForgeCodexPrApi:
    github: CodexPrApi
    gitlab: CodexPrApi

    def list_open_self_prs(self, *, slug: str) -> list[PrSummary]:
        forge = forge_for_repo_slug(slug)
        if forge == "gitlab":
            return self.gitlab.list_open_self_prs(slug=slug)
        return self.github.list_open_self_prs(slug=slug)
