"""``draft_state`` — refuse a review-request broadcast for a DRAFT MR (#1084 follow-up).

The gate is three-valued and fails CLOSED: only a code-host-CONFIRMED
``NOT_DRAFT`` clears the broadcast, so an unanswerable probe can never disarm the
"mark it Draft to hold the batch" mechanism.
"""

from unittest.mock import patch

from teatree.core.backend_protocols import DraftState
from teatree.core.gates.review_request_draft_gate import draft_refusal_reason, draft_state

_MR_URL = "https://gitlab.com/org/repo/-/merge_requests/385"
_FACTORY = "teatree.core.backend_factory.code_host_from_overlay"


class _Host:
    def __init__(self, *, answer: DraftState | Exception) -> None:
        self._answer = answer

    def fetch_pr_draft_state(self, *, slug: str, pr_id: int) -> DraftState:
        _ = (slug, pr_id)
        if isinstance(self._answer, Exception):
            raise self._answer
        return self._answer


class TestDraftState:
    def test_confirmed_draft(self) -> None:
        with patch(_FACTORY, return_value=_Host(answer=DraftState.DRAFT)):
            assert draft_state(_MR_URL) is DraftState.DRAFT

    def test_confirmed_non_draft(self) -> None:
        with patch(_FACTORY, return_value=_Host(answer=DraftState.NOT_DRAFT)):
            assert draft_state(_MR_URL) is DraftState.NOT_DRAFT

    def test_unparsable_url_is_unknown(self) -> None:
        with patch(_FACTORY, return_value=_Host(answer=DraftState.DRAFT)) as factory:
            assert draft_state("not-a-forge-url") is DraftState.UNKNOWN
        factory.assert_not_called()

    def test_no_host_is_unknown(self) -> None:
        with patch(_FACTORY, return_value=None):
            assert draft_state(_MR_URL) is DraftState.UNKNOWN

    def test_absent_forge_cli_is_unknown_not_non_draft(self) -> None:
        """The live deploy-image failure: no ``glab`` binary, so every probe raised.

        Swallowing it to "not a draft" made every MR in the image read as ready to
        broadcast — the batch the user held back with a Draft flag fired anyway.
        """
        missing_cli = FileNotFoundError(2, "No such file or directory", "glab")
        with patch(_FACTORY, return_value=_Host(answer=missing_cli)):
            assert draft_state(_MR_URL) is DraftState.UNKNOWN

    def test_named_overlay_selects_the_probing_credentials(self) -> None:
        """The probe must reach the named overlay's forge, not the ambient default.

        On the in-process MCP surface every overlay is registered and no
        ``T3_OVERLAY_NAME`` is exported, so an ambient
        ``code_host_from_overlay()`` resolves no host at all.
        """
        with patch(_FACTORY, return_value=_Host(answer=DraftState.DRAFT)) as factory:
            assert draft_state(_MR_URL, overlay_name="t3-acme") is DraftState.DRAFT
        factory.assert_called_once_with("t3-acme")

    def test_blank_overlay_keeps_the_ambient_default(self) -> None:
        with patch(_FACTORY, return_value=_Host(answer=DraftState.NOT_DRAFT)) as factory:
            assert draft_state(_MR_URL) is DraftState.NOT_DRAFT
        factory.assert_called_once_with(None)


class TestDraftRefusalReason:
    def test_confirmed_non_draft_is_postable(self) -> None:
        with patch(_FACTORY, return_value=_Host(answer=DraftState.NOT_DRAFT)):
            assert draft_refusal_reason(_MR_URL) == ""

    def test_draft_refuses_as_draft_mr(self) -> None:
        with patch(_FACTORY, return_value=_Host(answer=DraftState.DRAFT)):
            assert draft_refusal_reason(_MR_URL) == "draft_mr"

    def test_unknown_refuses_as_draft_state_unknown(self) -> None:
        with patch(_FACTORY, return_value=None):
            assert draft_refusal_reason(_MR_URL) == "draft_state_unknown"
