"""Concrete backend provider registered into ``core.backend_registry`` (#1922).

Owns every concrete-class construction ``core`` used to import directly: the
loader functions, the GitHub/GitLab/Slack clients, the sync backends, and the
Slack review-history read. ``BackendsConfig.ready()`` registers one instance so
``core`` reaches these capabilities through the registry, never an import.
"""

from typing import TYPE_CHECKING

from teatree.backends import loader
from teatree.backends.github import GitHubCodeHost
from teatree.backends.github import sync as github_sync
from teatree.backends.gitlab import GitLabCodeHost
from teatree.backends.gitlab import sync as gitlab_sync
from teatree.backends.notion import NotionClient
from teatree.backends.sentry import SentryClient
from teatree.backends.slack import (
    SlackReviewSearchRequest,
    SlackThreadActivityRequest,
    read_recent_review_matches,
    read_thread_activity,
)
from teatree.backends.slack.bot import SlackBotBackend
from teatree.core.backend_registry import register_backend_provider

if TYPE_CHECKING:
    from teatree.core.backend_protocols import CIService, CodeHostBackend, MessagingBackend
    from teatree.core.backend_registry import (
        NotionPageClient,
        ReviewHistoryReadLike,
        ReviewSearchSpec,
        SentryReadClient,
        ThreadActivityReadLike,
        ThreadActivitySpec,
    )
    from teatree.core.overlay import OverlayBase
    from teatree.types import SyncBackend


class ConcreteBackendProvider:
    def get_code_host(self, overlay: "OverlayBase") -> "CodeHostBackend | None":  # noqa: PLR6301 — fail-safe provider seam: instance method by Protocol contract
        return loader.get_code_host(overlay)

    def get_code_host_for_repo(self, overlay: "OverlayBase", repo_path: str) -> "CodeHostBackend | None":  # noqa: PLR6301 — fail-safe provider seam: instance method by Protocol contract
        return loader.get_code_host_for_repo(overlay, repo_path)

    def get_code_host_for_url(self, overlay: "OverlayBase", issue_url: str) -> "CodeHostBackend | None":  # noqa: PLR6301 — fail-safe provider seam: instance method by Protocol contract
        return loader.get_code_host_for_url(overlay, issue_url)

    def get_code_hosts(self, overlay: "OverlayBase") -> "list[CodeHostBackend]":  # noqa: PLR6301 — fail-safe provider seam: instance method by Protocol contract
        return loader.get_code_hosts(overlay)

    def get_messaging(self, overlay: "OverlayBase") -> "MessagingBackend | None":  # noqa: PLR6301 — fail-safe provider seam: instance method by Protocol contract
        return loader.get_messaging(overlay)

    def get_ci_service(self, *, gitlab_token: str, gitlab_url: str) -> "CIService | None":  # noqa: PLR6301 — fail-safe provider seam: instance method by Protocol contract
        return loader.get_ci_service(gitlab_token=gitlab_token, gitlab_url=gitlab_url)

    def reset_caches(self) -> None:  # noqa: PLR6301 — fail-safe provider seam: instance method by Protocol contract
        loader.reset_backend_caches()

    def build_github_host(self, *, token: str) -> "CodeHostBackend":  # noqa: PLR6301 — fail-safe provider seam: instance method by Protocol contract
        return GitHubCodeHost(token=token)

    def build_gitlab_host(self, *, token: str, base_url: str) -> "CodeHostBackend":  # noqa: PLR6301 — fail-safe provider seam: instance method by Protocol contract
        return GitLabCodeHost(token=token, base_url=base_url)

    def build_slack_messaging(  # noqa: PLR6301 — fail-safe provider seam: instance method by Protocol contract
        self,
        *,
        bot_token: str,
        app_token: str,
        user_token: str,
        user_id: str,
        dm_channel_id: str,
    ) -> "MessagingBackend":
        return SlackBotBackend(
            bot_token=bot_token,
            app_token=app_token,
            user_token=user_token,
            user_id=user_id,
            dm_channel_id=dm_channel_id,
            degrade_bad_user_token=True,
        )

    def build_sync_backends(self) -> "list[SyncBackend]":  # noqa: PLR6301 — fail-safe provider seam: instance method by Protocol contract
        return [github_sync.GitHubSyncBackend(), gitlab_sync.GitLabSyncBackend()]

    def build_notion_client(self, *, token: str) -> "NotionPageClient | None":  # noqa: PLR6301 — BackendProvider protocol method
        return NotionClient(token=token)

    def build_sentry_client(self, *, token: str, org: str, base_url: str) -> "SentryReadClient | None":  # noqa: PLR6301 — BackendProvider protocol method
        return SentryClient(token=token, org=org, base_url=base_url)

    def read_recent_review_matches(self, spec: "ReviewSearchSpec") -> "ReviewHistoryReadLike":  # noqa: PLR6301 — fail-safe provider seam: instance method by Protocol contract
        return read_recent_review_matches(
            SlackReviewSearchRequest(
                token=spec.token,
                channel_id=spec.channel_id,
                channel_name=spec.channel_name,
                pr_urls=spec.pr_urls,
                max_pages=spec.max_pages,
                oldest_ts=spec.oldest_ts,
                timeout=spec.timeout,
            ),
        )

    def read_thread_activity(self, spec: "ThreadActivitySpec") -> "ThreadActivityReadLike":  # noqa: PLR6301 — fail-safe provider seam: instance method by Protocol contract
        return read_thread_activity(
            SlackThreadActivityRequest(
                token=spec.token,
                channel_id=spec.channel_id,
                thread_ts=spec.thread_ts,
                timeout=spec.timeout,
            ),
        )


def install_backend_provider() -> None:
    register_backend_provider(ConcreteBackendProvider())
