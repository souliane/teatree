"""Sentry error tracking backend."""

from typing import cast

import httpx

from teatree.backends.http_retry import SimpleRetryTransport
from teatree.types import RawAPIDict


class SentryClient:
    """Sentry API client — fetches issue summaries for retro/triage skills.

    Reads use the shared bounded-retry transport. Issue resolution reads first,
    sends one non-replayed write, then verifies the status by re-read.
    """

    def __init__(self, *, token: str, org: str, base_url: str = "https://sentry.io") -> None:
        self.token = token
        self.org = org
        self.base_url = base_url.rstrip("/")
        self._transport = SimpleRetryTransport(env_prefix="T3_SENTRY_HTTP")

    def get_top_issues(self, *, project: str, limit: int = 10) -> list[dict[str, object]]:
        with self._client() as client:
            response = self._transport.run(
                lambda: client.get(
                    f"/api/0/projects/{self.org}/{project}/issues/",
                    params={"query": "is:unresolved", "sort": "freq", "limit": limit},
                ),
                idempotent=True,
            )
            response.raise_for_status()
            return cast("list[dict[str, object]]", response.json())

    def get_issue(self, issue_id: str) -> dict[str, object]:
        with self._client() as client:
            response = self._transport.run(lambda: client.get(f"/api/0/issues/{issue_id}/"), idempotent=True)
            response.raise_for_status()
            return cast("dict[str, object]", response.json())

    def resolve_issue(self, issue_id: str) -> RawAPIDict:
        current = self.get_issue(issue_id)
        if current.get("status") == "resolved":
            return current
        with self._client() as client:
            response = self._transport.run(
                lambda: client.put(f"/api/0/organizations/{self.org}/issues/{issue_id}/", json={"status": "resolved"}),
                idempotent=False,
            )
            response.raise_for_status()
        verified = self.get_issue(issue_id)
        if verified.get("status") != "resolved":
            msg = f"Sentry issue {issue_id} did not read back as resolved"
            raise RuntimeError(msg)
        return verified

    def get_issue_events(self, issue_id: str, *, limit: int = 10) -> list[dict[str, object]]:
        with self._client() as client:
            response = self._transport.run(
                lambda: client.get(f"/api/0/issues/{issue_id}/events/", params={"limit": limit}),
                idempotent=True,
            )
            response.raise_for_status()
            return cast("list[dict[str, object]]", response.json())

    def list_projects(self) -> list[dict[str, object]]:
        with self._client() as client:
            response = self._transport.run(
                lambda: client.get(f"/api/0/organizations/{self.org}/projects/"), idempotent=True
            )
            response.raise_for_status()
            return cast("list[dict[str, object]]", response.json())

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=15.0,
        )
