"""A fake of api.github.com for the approval tests."""

import json

import httpx


class FakeGitHub:
    """The forge HTTP boundary: records every request and answers like api.github.com."""

    def __init__(
        self, login: str = "alice", *, post_status: int = 200, recorded_state: str = "APPROVED", user_status: int = 200
    ) -> None:
        self.login = login
        self.user_status = user_status
        self.post_status = post_status
        self.recorded_state = recorded_state
        self.requests: list[tuple[str, str, dict[str, str], object]] = []
        self.reviews: list[dict[str, object]] = []

    def request(
        self, method: str, url: str, *, headers: dict[str, str], payload: object = None, timeout: float | None = None
    ) -> httpx.Response:
        _ = timeout
        self.requests.append((method, url, headers, payload))
        request = httpx.Request(method, url)
        path = httpx.URL(url).path
        if method == "POST" and path.endswith("/pulls/7/reviews"):
            if self.post_status != httpx.codes.OK:
                return httpx.Response(
                    self.post_status, json={"message": "Can not approve your own pull request"}, request=request
                )
            self.reviews.append({"user": {"login": self.login}, "state": self.recorded_state})
            return httpx.Response(200, json={"id": 1, "state": self.recorded_state}, request=request)
        if path.endswith("/pulls/7/reviews"):
            return httpx.Response(200, json=self.reviews, request=request)
        if path == "/user":
            if self.user_status != httpx.codes.OK:
                return httpx.Response(self.user_status, json={"message": "Bad credentials"}, request=request)
            return httpx.Response(200, json={"login": self.login}, request=request)
        return httpx.Response(200, json={"requested_reviewers": []}, request=request)

    @property
    def review_posts(self) -> list[dict[str, object]]:
        return [
            json.loads(str(payload))
            for method, url, _headers, payload in self.requests
            if method == "POST" and url.endswith("/pulls/7/reviews")
        ]

    @property
    def bearer_tokens(self) -> set[str]:
        return {headers["Authorization"] for _method, _url, headers, _payload in self.requests}
