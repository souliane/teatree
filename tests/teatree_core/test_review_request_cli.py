"""``t3 review-request`` overlay routing — ``_active_project`` (#1103).

Bug B: the typer ``post``/``check`` delegates resolved the managepy
target via ``discover_active_overlay()``, which the cwd-``manage.py``
discovery resolves to the clone the agent runs from (→ the teatree
project when run from the teatree repo) — so a review-request post for a
*different* configured overlay could not resolve that overlay's Connect
channel/token. The fix routes through ``config._active_overlay_entry``
so ``T3_OVERLAY_NAME`` wins first (matching ``get_overlay()``), with the
cwd-``manage.py`` developer fallback preserved.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.cli.review_request import _active_project
from teatree.config import OverlayEntry

_TEATREE_PATH = Path("/workspace/teatree")
_OTHER_PATH = Path("/workspace/acme-overlay")
_OTHER_NAME = "acme-overlay"


def _two_overlays() -> list[OverlayEntry]:
    return [
        OverlayEntry(name="teatree", overlay_class="", project_path=_TEATREE_PATH),
        OverlayEntry(name=_OTHER_NAME, overlay_class="", project_path=_OTHER_PATH),
    ]


class TestActiveProjectOverlayRouting:
    def test_env_overlay_name_routes_to_that_overlay(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``T3_OVERLAY_NAME`` selects the overlay, NOT the cwd manage.py."""
        monkeypatch.setenv("T3_OVERLAY_NAME", _OTHER_NAME)
        with patch("teatree.config.discover_overlays", return_value=_two_overlays()):
            project, name = _active_project()
        assert (project, name) == (_OTHER_PATH, _OTHER_NAME)

    def test_post_delegate_threads_resolved_overlay_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``t3 review-request post`` runs managepy with the resolved overlay."""
        monkeypatch.setenv("T3_OVERLAY_NAME", _OTHER_NAME)
        with (
            patch("teatree.config.discover_overlays", return_value=_two_overlays()),
            patch("teatree.cli.review_request.managepy") as managepy,
        ):
            from teatree.cli.review_request import post  # noqa: PLC0415

            post(mr_url="https://gitlab.com/org/repo/-/merge_requests/385", approver="souliane", title="")
        assert managepy.call_args.kwargs["overlay_name"] == _OTHER_NAME
        assert managepy.call_args.args[0] == _OTHER_PATH
