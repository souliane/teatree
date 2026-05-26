"""``t3 review-request`` overlay routing — ``_active_project`` (#1103, #1312).

Bug B (#1103): the typer ``post``/``check`` delegates resolved the managepy
target via ``discover_active_overlay()``, which the cwd-``manage.py``
discovery resolves to the clone the agent runs from (→ the teatree
project when run from the teatree repo) — so a review-request post for a
*different* configured overlay could not resolve that overlay's Connect
channel/token. The fix routes through ``config._active_overlay_entry``
so ``T3_OVERLAY_NAME`` wins first (matching ``get_overlay()``), with the
cwd-``manage.py`` developer fallback preserved.

Bug (#1312): the same delegates then handed the resolved overlay
``project_path`` to :func:`managepy`, which prefers the overlay's own
``manage.py`` when one exists. ``followup`` / ``review_request_check`` /
``review_request_post`` are teatree-CORE management commands and an
overlay's ``manage.py`` (running against its own settings module) has
no such commands, so both crashed with ``Unknown command: 'followup'``.
The fix routes them via :func:`managepy_core` so dispatch is always
``python -m teatree`` regardless of overlay project path.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

from teatree.cli.review_request import _active_project, review_request_app
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
        """``t3 review-request post`` runs the core dispatch with the resolved overlay."""
        monkeypatch.setenv("T3_OVERLAY_NAME", _OTHER_NAME)
        with (
            patch("teatree.config.discover_overlays", return_value=_two_overlays()),
            patch("teatree.cli.review_request.managepy_core") as managepy_core,
        ):
            from teatree.cli.review_request import post  # noqa: PLC0415

            post(mr_url="https://gitlab.com/org/repo/-/merge_requests/385", approver="souliane", title="")
        assert managepy_core.call_args.kwargs["overlay_name"] == _OTHER_NAME


class TestCoreDispatch:
    """Both ``discover`` and ``post`` use teatree-core dispatch (#1312).

    The bug: running ``t3 review-request {discover,post}`` from an overlay
    clone whose ``manage.py`` runs against its own settings module crashed
    with ``CommandFailedError`` because the call was routed through the
    overlay's ``manage.py``, which has no ``followup`` /
    ``review_request_post`` commands. Both commands MUST use
    ``python -m teatree`` dispatch regardless of resolved project path.
    """

    def test_discover_uses_managepy_core(self) -> None:
        with patch("teatree.cli.review_request.managepy_core") as managepy_core:
            from teatree.cli.review_request import discover  # noqa: PLC0415

            discover()
        assert managepy_core.call_args.args == ("followup", "discover-mrs")

    def test_check_uses_managepy_core(self) -> None:
        with patch("teatree.cli.review_request.managepy_core") as managepy_core:
            from teatree.cli.review_request import check  # noqa: PLC0415

            check(mr_url="https://gitlab.com/org/repo/-/merge_requests/385")
        assert managepy_core.call_args.args[0] == "review_request_check"

    def test_post_uses_managepy_core(self) -> None:
        with patch("teatree.cli.review_request.managepy_core") as managepy_core:
            from teatree.cli.review_request import post  # noqa: PLC0415

            post(mr_url="https://gitlab.com/org/repo/-/merge_requests/385", approver="souliane", title="")
        assert managepy_core.call_args.args[0] == "review_request_post"

    def test_discover_via_cli_runner_uses_core_dispatch(self) -> None:
        """End-to-end via CliRunner: discover routes through teatree-core dispatch (#1312).

        Regression test: before the fix, this invocation crashed with
        ``CommandFailedError`` from the overlay's ``manage.py``. Now it
        completes by calling :func:`managepy_core` (teatree-native
        dispatch), independent of any project path. The patch target is
        ``teatree.cli.overlay.run_streamed`` to catch BOTH dispatch paths
        — :func:`managepy_core` (teatree-native) and the would-be-buggy
        :func:`managepy` (overlay ``manage.py``) — at the same chokepoint
        so the test fails either way the bug regresses.
        """
        runner = CliRunner()
        app = typer.Typer()
        app.add_typer(review_request_app, name="review-request")
        with patch("teatree.cli.overlay.run_streamed") as run_streamed:
            result = runner.invoke(app, ["review-request", "discover"])
        assert result.exit_code == 0, result.output
        assert run_streamed.called
        cmd = run_streamed.call_args.args[0]
        assert "-m" in cmd, f"discover must use python -m teatree dispatch, got {cmd!r}"
        assert "teatree" in cmd, f"discover must use python -m teatree dispatch, got {cmd!r}"
        assert "manage.py" not in " ".join(cmd), f"discover must NOT route through overlay manage.py, got {cmd!r}"
