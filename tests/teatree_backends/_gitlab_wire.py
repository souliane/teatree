"""A real :class:`GitLabAPI` whose only fake is the HTTP GET — the TTL caches, pagination and parsing all run."""

import httpx

from teatree.backends.gitlab.api import GitLabAPI


class GitLabWire(GitLabAPI):
    """Answers each GET from *routes* keyed by path (query dropped); *failures* answer a status; anything else 404s."""

    def __init__(self, routes: dict[str, object], *, failures: dict[str, int] | None = None) -> None:
        super().__init__(token="wire-token", base_url="https://gitlab.example/api/v4")
        self.routes = routes
        self.failures = failures or {}
        self.paths: list[str] = []

    def _get(self, endpoint: str, *, timeout: float | None = None) -> httpx.Response:
        del timeout
        path = endpoint.partition("?")[0]
        self.paths.append(path)
        request = httpx.Request("GET", self._url(endpoint))
        if path in self.failures:
            return httpx.Response(self.failures[path], json={"message": "wire failure"}, request=request)
        if path not in self.routes:
            return httpx.Response(httpx.codes.NOT_FOUND, json={"message": "404 Not Found"}, request=request)
        return httpx.Response(httpx.codes.OK, json=self.routes[path], request=request)
