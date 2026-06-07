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
    from teatree.types import SyncBackend


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

    def read_recent_review_matches(self, spec: ReviewSearchSpec) -> ReviewHistoryReadLike: ...  # pragma: no branch


class _UnconfiguredProvider:
    """Fail-safe provider used before the backends app registers the real one."""

    def get_code_host(self, overlay: "OverlayBase") -> "CodeHostBackend | None":  # noqa: ARG002, PLR6301
        return None

    def get_code_host_for_repo(self, overlay: "OverlayBase", repo_path: str) -> "CodeHostBackend | None":  # noqa: ARG002, PLR6301
        return None

    def get_code_hosts(self, overlay: "OverlayBase") -> "list[CodeHostBackend]":  # noqa: ARG002, PLR6301
        return []

    def get_messaging(self, overlay: "OverlayBase") -> "MessagingBackend | None":  # noqa: ARG002, PLR6301
        return None

    def get_ci_service(self, *, gitlab_token: str, gitlab_url: str) -> "CIService | None":  # noqa: ARG002, PLR6301
        return None

    def reset_caches(self) -> None:  # noqa: PLR6301
        return

    def build_github_host(self, *, token: str) -> "CodeHostBackend":  # noqa: ARG002, PLR6301
        msg = "no backend provider registered — teatree.backends app is not installed"
        raise RuntimeError(msg)

    def build_gitlab_host(self, *, token: str, base_url: str) -> "CodeHostBackend":  # noqa: ARG002, PLR6301
        msg = "no backend provider registered — teatree.backends app is not installed"
        raise RuntimeError(msg)

    def build_slack_messaging(  # noqa: PLR6301
        self,
        *,
        bot_token: str,  # noqa: ARG002
        app_token: str,  # noqa: ARG002
        user_token: str,  # noqa: ARG002
        user_id: str,  # noqa: ARG002
        dm_channel_id: str,  # noqa: ARG002
    ) -> "MessagingBackend":
        msg = "no backend provider registered — teatree.backends app is not installed"
        raise RuntimeError(msg)

    def build_sync_backends(self) -> "list[SyncBackend]":  # noqa: PLR6301
        return []

    def read_recent_review_matches(self, spec: ReviewSearchSpec) -> ReviewHistoryReadLike:  # noqa: ARG002, PLR6301
        return _EmptyReviewHistoryRead()


class _EmptyReviewHistoryRead:
    ok = False
    matches: list[ReviewMatchLike] = []  # noqa: RUF012


_UNCONFIGURED: BackendProvider = _UnconfiguredProvider()
_provider: BackendProvider | None = None


def register_backend_provider(provider: BackendProvider) -> None:
    global _provider  # noqa: PLW0603 — single process-wide provider registered at app-ready
    _provider = provider


def get_backend_provider() -> BackendProvider:
    return _provider if _provider is not None else _UNCONFIGURED
