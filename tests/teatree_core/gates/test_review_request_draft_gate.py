"""``is_draft_mr`` — refuse a review-request broadcast for a DRAFT MR (#1084 follow-up)."""

from unittest.mock import patch

from teatree.core.gates.review_request_draft_gate import is_draft_mr

_MR_URL = "https://gitlab.com/org/repo/-/merge_requests/385"
_FACTORY = "teatree.core.backend_factory.code_host_from_overlay"


class _Host:
    def __init__(self, *, draft: bool | Exception) -> None:
        self._draft = draft

    def fetch_pr_is_draft(self, *, slug: str, pr_id: int) -> bool:
        _ = (slug, pr_id)
        if isinstance(self._draft, Exception):
            raise self._draft
        return self._draft


class TestIsDraftMr:
    def test_confirmed_draft_is_true(self) -> None:
        with patch(_FACTORY, return_value=_Host(draft=True)):
            assert is_draft_mr(_MR_URL) is True

    def test_non_draft_is_false(self) -> None:
        with patch(_FACTORY, return_value=_Host(draft=False)):
            assert is_draft_mr(_MR_URL) is False

    def test_unparsable_url_is_false(self) -> None:
        with patch(_FACTORY, return_value=_Host(draft=True)) as factory:
            assert is_draft_mr("not-a-forge-url") is False
        factory.assert_not_called()

    def test_no_host_is_false(self) -> None:
        with patch(_FACTORY, return_value=None):
            assert is_draft_mr(_MR_URL) is False

    def test_read_error_fails_open_to_false(self) -> None:
        with patch(_FACTORY, return_value=_Host(draft=RuntimeError("boom"))):
            assert is_draft_mr(_MR_URL) is False

    def test_named_overlay_selects_the_probing_credentials(self) -> None:
        """The probe must reach the named overlay's forge, not the ambient default.

        On the in-process MCP surface every overlay is registered and no
        ``T3_OVERLAY_NAME`` is exported, so an ambient
        ``code_host_from_overlay()`` resolves no host at all and a genuinely
        DRAFT MR reads as "not a draft" — the gate silently stops firing.
        """
        with patch(_FACTORY, return_value=_Host(draft=True)) as factory:
            assert is_draft_mr(_MR_URL, overlay_name="t3-acme") is True
        factory.assert_called_once_with("t3-acme")

    def test_blank_overlay_keeps_the_ambient_default(self) -> None:
        with patch(_FACTORY, return_value=_Host(draft=False)) as factory:
            assert is_draft_mr(_MR_URL) is False
        factory.assert_called_once_with(None)
