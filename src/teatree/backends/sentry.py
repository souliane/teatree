"""Sentry error tracking backend."""

from typing import cast

import httpx


class SentryClient:
    """Sentry API client — fetches issue summaries for retro/triage skills."""

    def __init__(self, *, token: str, org: str, base_url: str = "https://sentry.io") -> None:
        self.token = token
        self.org = org
        self.base_url = base_url.rstrip("/")

    def get_top_issues(self, *, project: str, limit: int = 10) -> list[dict[str, object]]:
        with self._client() as client:
            response = client.get(
                f"/api/0/projects/{self.org}/{project}/issues/",
                params={"query": "is:unresolved", "sort": "freq", "limit": limit},
            )
            response.raise_for_status()
            return cast("list[dict[str, object]]", response.json())

    def get_issue(self, issue_id: str) -> dict[str, object]:
        with self._client() as client:
            response = client.get(f"/api/0/issues/{issue_id}/")
            response.raise_for_status()
            return cast("dict[str, object]", response.json())

    def get_issue_events(self, issue_id: str, *, limit: int = 10) -> list[dict[str, object]]:
        with self._client() as client:
            response = client.get(
                f"/api/0/issues/{issue_id}/events/",
                params={"limit": limit},
            )
            response.raise_for_status()
            return cast("list[dict[str, object]]", response.json())

    def list_projects(self) -> list[dict[str, object]]:
        with self._client() as client:
            response = client.get(f"/api/0/organizations/{self.org}/projects/")
            response.raise_for_status()
            return cast("list[dict[str, object]]", response.json())

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=15.0,
        )
