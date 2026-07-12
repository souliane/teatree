"""Registry for the backend builder/loader seam — the last core → backends cut (#1922).

``core.backend_factory`` (the #195 overlay-aware factory), ``core.sync``,
``core.overlay`` and ``core.review_request_guard`` all need to *build* concrete
backends or run a backend capability, but the builders live in
``teatree.backends`` (the loader + concrete clients). Rather than ``core``
importing ``backends``, ``backends`` registers one :class:`BackendProvider` here
at app-ready time and ``core`` resolves it.

Fail-SAFE: an unregistered provider returns ``None`` / empty for every build (the
same shape as "no credentials configured"), so a bare ``django.setup()`` without
the backends app never crashes — it simply builds no backends.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from teatree.core.backend_protocols import CIService, CodeHostBackend, MessagingBackend
    from teatree.core.overlay import OverlayBase
    from teatree.types import RawAPIDict, SyncBackend


@dataclass(frozen=True, slots=True)
class ReviewSearchSpec:
    """Recency-bounded Slack channel-history read request (core-owned shape)."""

    token: str
    channel_id: str
    channel_name: str
    pr_urls: list[str]
    max_pages: int
    oldest_ts: str
    timeout: float


class ReviewHistoryReadLike(Protocol):
    @property
    def ok(self) -> bool: ...  # pragma: no branch

    @property
    def matches(self) -> "list[ReviewMatchLike]": ...  # pragma: no branch


class ReviewMatchLike(Protocol):
    pr_url: str
    ts: str
    author: str
    permalink: str


class NotionPageClient(Protocol):
    """Core-owned view of the direct Notion API client the backends app builds.

    ``core.sync`` reads a page's status (and, gated by ``notion_write_back``,
    writes it back) without importing the concrete ``teatree.backends.notion``
    client — the same core → backends inversion as the other provider builders.
    """

    def get_page_status(self, page_id: str, *, property_name: str = "Status") -> str | None: ...  # pragma: no branch

    def update_page_status(
        self, page_id: str, *, property_name: str, value: str
    ) -> "RawAPIDict": ...  # pragma: no branch


class SentryReadClient(Protocol):
    """Core-owned view of the read-only Sentry client the backends app builds.

    The MCP sentry tool group reads issues/events/projects without importing the
    concrete ``teatree.backends.sentry`` client — the same core → backends
    inversion as :class:`NotionPageClient` and the forge/messaging builders.
    """

    def get_top_issues(self, *, project: str, limit: int = 10) -> "list[RawAPIDict]": ...  # pragma: no branch

    def get_issue(self, issue_id: str) -> "RawAPIDict": ...  # pragma: no branch

    def get_issue_events(self, issue_id: str, *, limit: int = 10) -> "list[RawAPIDict]": ...  # pragma: no branch

    def list_projects(self) -> "list[RawAPIDict]": ...  # pragma: no branch


class BackendProvider(Protocol):
    def get_code_host(self, overlay: "OverlayBase") -> "CodeHostBackend | None": ...  # pragma: no branch

    def get_code_host_for_repo(
        self, overlay: "OverlayBase", repo_path: str
    ) -> "CodeHostBackend | None": ...  # pragma: no branch

    def get_code_hosts(self, overlay: "OverlayBase") -> "list[CodeHostBackend]": ...  # pragma: no branch

    def get_messaging(self, overlay: "OverlayBase") -> "MessagingBackend | None": ...  # pragma: no branch

    def get_ci_service(self, *, gitlab_token: str, gitlab_url: str) -> "CIService | None": ...  # pragma: no branch

    def reset_caches(self) -> None: ...  # pragma: no branch

    def build_github_host(self, *, token: str) -> "CodeHostBackend": ...  # pragma: no branch

    def build_gitlab_host(self, *, token: str, base_url: str) -> "CodeHostBackend": ...  # pragma: no branch

    def build_slack_messaging(
        self,
        *,
        bot_token: str,
        app_token: str,
        user_token: str,
        user_id: str,
        dm_channel_id: str,
    ) -> "MessagingBackend": ...  # pragma: no branch

    def build_sync_backends(self) -> "list[SyncBackend]": ...  # pragma: no branch

    def build_notion_client(self, *, token: str) -> "NotionPageClient | None": ...  # pragma: no branch

    def build_sentry_client(
        self, *, token: str, org: str, base_url: str
    ) -> "SentryReadClient | None": ...  # pragma: no branch

    def read_recent_review_matches(self, spec: ReviewSearchSpec) -> ReviewHistoryReadLike: ...  # pragma: no branch


class _UnconfiguredProvider:
    """Fail-safe provider used before the backends app registers the real one."""

    def get_code_host(self, overlay: "OverlayBase") -> "CodeHostBackend | None":  # noqa: ARG002, PLR6301 — fail-safe provider seam: instance method by Protocol contract; args used by real overrides
        return None

    def get_code_host_for_repo(self, overlay: "OverlayBase", repo_path: str) -> "CodeHostBackend | None":  # noqa: ARG002, PLR6301 — fail-safe provider seam: instance method by Protocol contract; args used by real overrides
        return None

    def get_code_hosts(self, overlay: "OverlayBase") -> "list[CodeHostBackend]":  # noqa: ARG002, PLR6301 — fail-safe provider seam: instance method by Protocol contract; args used by real overrides
        return []

    def get_messaging(self, overlay: "OverlayBase") -> "MessagingBackend | None":  # noqa: ARG002, PLR6301 — fail-safe provider seam: instance method by Protocol contract; args used by real overrides
        return None

    def get_ci_service(self, *, gitlab_token: str, gitlab_url: str) -> "CIService | None":  # noqa: ARG002, PLR6301 — fail-safe provider seam: instance method by Protocol contract; args used by real overrides
        return None

    def reset_caches(self) -> None:  # noqa: PLR6301 — fail-safe provider seam: instance method by Protocol contract
        return

    def build_github_host(self, *, token: str) -> "CodeHostBackend":  # noqa: ARG002, PLR6301 — fail-safe provider seam: instance method by Protocol contract; args used by real overrides
        msg = "no backend provider registered — teatree.backends app is not installed"
        raise RuntimeError(msg)

    def build_gitlab_host(self, *, token: str, base_url: str) -> "CodeHostBackend":  # noqa: ARG002, PLR6301 — fail-safe provider seam: instance method by Protocol contract; args used by real overrides
        msg = "no backend provider registered — teatree.backends app is not installed"
        raise RuntimeError(msg)

    def build_slack_messaging(  # noqa: PLR6301 — fail-safe provider seam: instance method by Protocol contract
        self,
        *,
        bot_token: str,  # noqa: ARG002 — unused in this default seam; the concrete provider consumes it
        app_token: str,  # noqa: ARG002 — unused in this default seam; the concrete provider consumes it
        user_token: str,  # noqa: ARG002 — unused in this default seam; the concrete provider consumes it
        user_id: str,  # noqa: ARG002 — unused in this default seam; the concrete provider consumes it
        dm_channel_id: str,  # noqa: ARG002 — unused in this default seam; the concrete provider consumes it
    ) -> "MessagingBackend":
        msg = "no backend provider registered — teatree.backends app is not installed"
        raise RuntimeError(msg)

    def build_sync_backends(self) -> "list[SyncBackend]":  # noqa: PLR6301 — fail-safe provider seam: instance method by Protocol contract
        return []

    def build_notion_client(self, *, token: str) -> "NotionPageClient | None":  # noqa: ARG002, PLR6301 — fail-safe provider seam: instance method by Protocol contract; args used by real overrides
        return None

    def build_sentry_client(self, *, token: str, org: str, base_url: str) -> "SentryReadClient | None":  # noqa: ARG002, PLR6301 — fail-safe protocol stub; args unused, returns None with no backends app
        return None

    def read_recent_review_matches(self, spec: ReviewSearchSpec) -> ReviewHistoryReadLike:  # noqa: ARG002, PLR6301 — fail-safe provider seam: instance method by Protocol contract; args used by real overrides
        return _EmptyReviewHistoryRead()


class _EmptyReviewHistoryRead:
    ok = False
    matches: list[ReviewMatchLike] = []  # noqa: RUF012 — fail-safe empty default, never a shared mutable class attribute


_UNCONFIGURED: BackendProvider = _UnconfiguredProvider()
_provider: BackendProvider | None = None


def register_backend_provider(provider: BackendProvider) -> None:
    global _provider  # noqa: PLW0603 — single process-wide provider registered at app-ready
    _provider = provider


def get_backend_provider() -> BackendProvider:
    return _provider if _provider is not None else _UNCONFIGURED
