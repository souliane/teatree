"""The collapsed-diff draft refusal must point at the authorized live path.

``post_draft_note_impl`` refuses when GitLab returns a null ``line_code`` —
the draft_notes API cannot anchor on a collapsed diff. Its guidance text is
the only thing between the caller and two wrong moves: re-running a plain
``post-comment`` (which IS the draft path, so it lands back on this same
refusal), or reading the escape as a licence to publish the whole review
live — one human authorization per finding instead of one for the batch.
"""

import re
from unittest.mock import MagicMock, patch

from teatree.cli.review.post_impl import post_draft_note_impl

_REPO = "org/repo"
_MR = 7
_FILE = "src/big_module.py"
_LINE = 4200


def _collapsed_diff_refusal() -> str:
    """Drive ``post_draft_note_impl`` into the null-``line_code`` branch and return its message."""
    service = MagicMock()
    service._resolve_base_url = lambda: "https://gitlab.example/api/v4"
    api = MagicMock()
    api.post_json.return_value = {"id": 51, "line_code": None}
    service._get_api.return_value = api

    with patch(
        "teatree.cli.review.post_impl.resolve_inline_position",
        return_value=({"base_sha": "a", "head_sha": "b", "start_sha": "c"}, None),
    ):
        msg, code = post_draft_note_impl(service, _REPO, _MR, "nit", file=_FILE, line=_LINE)

    assert code == 1, f"a null line_code must refuse the draft, got rc={code!r}, msg={msg!r}"
    return msg


class TestCollapsedDiffGuidance:
    """The refusal's escape route must be accurate about today's draft/live routing."""

    def test_escape_is_the_authorized_live_path(self) -> None:
        msg = _collapsed_diff_refusal()
        suggested = re.findall(r"`t3 review post-comment[^`]*`", msg)

        assert suggested, f"the refusal must suggest a concrete post-comment escape, got: {msg!r}"
        assert all("--live" in command for command in suggested), (
            f"a post-comment without --live IS the draft path and lands back on this refusal: {suggested!r}"
        )
        assert "t3 review authorize" in msg, f"the live path needs an authorization the message must name: {msg!r}"

    def test_does_not_call_a_plain_post_comment_non_draft(self) -> None:
        msg = _collapsed_diff_refusal()

        assert "non-draft" not in msg.lower(), (
            f"plain post-comment is the DRAFT path — calling it non-draft sends the caller live: {msg!r}"
        )

    def test_scopes_the_escape_to_this_file_and_keeps_the_rest_on_drafts(self) -> None:
        msg = _collapsed_diff_refusal().lower()

        assert "this file" in msg, f"the live escape covers only the collapsed file: {msg!r}"
        assert "draft path" in msg, f"every other finding stays on the draft path: {msg!r}"
