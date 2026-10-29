"""The five conditions a headless run must be able to tell apart."""

from collections.abc import Callable

import httpx
import pytest

from teatree.backends.notion import errors
from teatree.backends.notion.client import NotionClient
from teatree.backends.notion.errors import (
    NotionBadTokenError,
    NotionCapabilityDeniedError,
    NotionError,
    NotionNotSharedError,
    NotionObjectNotFoundError,
    NotionRateLimitedError,
    NotionWriteNotApprovedError,
    normalize_object_id,
)
from tests.teatree_backends.notion._fake_notion import UNSEEN_OBJECT_FRAGMENTS, FakeNotion

_PAGE = "11111111-1111-1111-1111-111111111111"


class TestNormalizeObjectId:
    @pytest.mark.parametrize(
        ("reference", "expected"),
        [
            ("11111111111111111111111111111111", _PAGE),
            (_PAGE, _PAGE),
            ("https://www.notion.so/Some-Title-11111111111111111111111111111111", _PAGE),
            ("https://www.notion.so/space/11111111111111111111111111111111?v=abc", _PAGE),
        ],
    )
    def test_extracts_the_dashed_id(self, reference: str, expected: str) -> None:
        assert normalize_object_id(reference) == expected

    def test_a_reference_carrying_no_id_is_a_proven_not_found(self) -> None:
        with pytest.raises(NotionObjectNotFoundError, match="carries no Notion object id"):
            normalize_object_id("https://example.test/not-a-notion-page")


class TestFailureClassification:
    def test_bad_token_is_reported_as_a_token_problem(self, notion: FakeNotion) -> None:
        notion.fail_with = (401, "unauthorized")
        notion.identity_fail_with = (401, "unauthorized")

        with pytest.raises(NotionBadTokenError, match="rejected the integration token"):
            NotionClient(token="stale").get_page(_PAGE)

    def test_unshared_page_names_the_integration_and_the_sharing_grant(self, notion: FakeNotion) -> None:
        notion.fail_with = (404, "object_not_found")

        with pytest.raises(NotionNotSharedError) as caught:
            NotionClient(token="good").get_page(_PAGE)

        message = str(caught.value)
        assert "not shared with this integration" in message
        assert "Factory" in message, "the human needs the integration NAME to grant access"
        assert "Connections" in message

    @pytest.mark.parametrize("fragment", UNSEEN_OBJECT_FRAGMENTS.values(), ids=UNSEEN_OBJECT_FRAGMENTS.keys())
    def test_an_unseen_page_states_what_the_api_knows_and_the_checks_that_separate_the_causes(
        self, notion: FakeNotion, fragment: str
    ) -> None:
        notion.fail_with = (404, "object_not_found")

        with pytest.raises(NotionNotSharedError) as caught:
            NotionClient(token="good").get_page(_PAGE)

        assert fragment in str(caught.value)

    def test_an_unseen_page_is_never_blamed_on_one_cause_nor_on_the_ui_showing_a_bot_id(
        self, notion: FakeNotion
    ) -> None:
        notion.fail_with = (404, "object_not_found")

        with pytest.raises(NotionNotSharedError) as caught:
            NotionClient(token="good").get_page(_PAGE)

        message = str(caught.value)
        assert "because" not in message
        assert f"{_PAGE} is not shared" not in message
        assert "by that bot id" not in message

    def test_a_404_under_a_rejected_token_reports_the_token_not_the_sharing(self, notion: FakeNotion) -> None:
        notion.fail_with = (404, "object_not_found")
        notion.identity_fail_with = (401, "unauthorized")

        with pytest.raises(NotionBadTokenError):
            NotionClient(token="stale").get_page(_PAGE)

    def test_missing_capability_is_distinct_from_missing_share(self, notion: FakeNotion) -> None:
        notion.fail_with = (403, "restricted_resource")

        with pytest.raises(NotionCapabilityDeniedError, match="lacks the capability"):
            NotionClient(token="good").list_comments(_PAGE)

    def test_malformed_id_is_a_proven_not_found_not_a_sharing_failure(self, notion: FakeNotion) -> None:
        notion.fail_with = (400, "validation_error")

        with pytest.raises(NotionObjectNotFoundError, match="validation_error"):
            NotionClient(token="good").get_page(_PAGE)

    def test_rate_limit_carries_the_retry_after(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/users/me"):
                return httpx.Response(200, json={"id": "bot-1", "name": "Factory"})
            return httpx.Response(429, headers={"Retry-After": "12"}, json={"code": "rate_limited"})

        _mock_transport(monkeypatch, handler)
        client = NotionClient(token="good")
        client._transport._sleep = lambda _: None

        with pytest.raises(NotionRateLimitedError) as caught:
            client.get_page(_PAGE)

        assert caught.value.retry_after == pytest.approx(12.0)
        assert caught.value.exit_code == 8

    def test_a_transient_5xx_stays_an_http_error_the_retry_layer_owns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mock_transport(monkeypatch, lambda _: httpx.Response(503))
        client = NotionClient(token="good")
        client._transport._sleep = lambda _: None

        with pytest.raises(httpx.HTTPStatusError):
            client.get_page(_PAGE)

    def test_every_condition_has_its_own_exit_code(self) -> None:
        codes = [
            NotionBadTokenError.exit_code,
            NotionCapabilityDeniedError.exit_code,
            NotionNotSharedError.exit_code,
            NotionObjectNotFoundError.exit_code,
            NotionRateLimitedError.exit_code,
        ]
        assert len(set(codes)) == len(codes)


class TestExitCodeTable:
    @staticmethod
    def _conditions() -> list[type[NotionError]]:
        return [
            cls
            for cls in vars(errors).values()
            if isinstance(cls, type) and issubclass(cls, NotionError) and cls is not NotionError
        ]

    def test_no_two_conditions_share_an_exit_code(self) -> None:
        codes = [cls.exit_code for cls in self._conditions()]

        assert len(set(codes)) == len(codes)

    def test_every_condition_is_named_in_the_documented_table(self) -> None:
        undocumented = [cls.__name__ for cls in self._conditions() if cls.__name__ not in (errors.__doc__ or "")]

        assert undocumented == []

    def test_a_write_without_a_recorded_approval_exits_23(self) -> None:
        assert NotionWriteNotApprovedError.exit_code == 23


def _mock_transport(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]) -> None:
    original = httpx.Client.__init__

    def patched(self: httpx.Client, **kwargs: object) -> None:
        kwargs["transport"] = httpx.MockTransport(handler)
        original(self, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched)
