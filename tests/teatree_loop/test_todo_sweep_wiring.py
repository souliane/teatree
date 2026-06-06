"""Wiring tests for the TODO-sweep scanner (#129).

Covers dispatch routing (``todo.*`` → handler/statusline), the config knobs +
defaults, and the per-overlay builder + escape hatch.
"""

from typing import Any
from unittest.mock import patch

from django.test import TestCase

from teatree.core.overlay import OverlayBase
from teatree.loop.dispatch import dispatch
from teatree.loop.scanners.base import ScanSignal


class _Overlay(OverlayBase):
    def get_repos(self) -> list[str]:
        return ["acme-repo"]

    def get_provision_steps(self, worktree: Any) -> list:
        _ = worktree
        return []


class DispatchRoutingTests(TestCase):
    def test_completion_detected_routes_to_mechanical(self) -> None:
        signal = ScanSignal(kind="todo.completion_detected", summary="done", payload={"task_id": 1})
        actions = dispatch([signal])
        assert len(actions) == 1
        assert actions[0].kind == "mechanical"
        assert actions[0].zone == "todo_completion"

    def test_orphaned_routes_to_action_needed_statusline(self) -> None:
        signal = ScanSignal(kind="todo.orphaned", summary="orphan", payload={"task_id": 1})
        actions = dispatch([signal])
        assert len(actions) == 1
        assert actions[0].kind == "statusline"
        assert actions[0].zone == "action_needed"

    def test_completion_payload_propagated(self) -> None:
        payload: dict[str, object] = {"task_id": 42, "ticket_id": 7, "issue_url": "http://x"}
        actions = dispatch([ScanSignal(kind="todo.completion_detected", summary="x", payload=payload)])
        assert actions[0].payload == payload


class ConfigDefaultsTests(TestCase):
    def _settings(self) -> object:
        from teatree.config import UserSettings  # noqa: PLC0415

        return UserSettings()

    def test_enabled_by_default(self) -> None:
        assert self._settings().todo_sweep_disabled is False

    def test_recheck_interval_default(self) -> None:
        assert self._settings().todo_sweep_recheck_interval_hours == 1

    def test_settings_are_overlay_overridable(self) -> None:
        from teatree.config import OVERLAY_OVERRIDABLE_SETTINGS  # noqa: PLC0415

        assert "todo_sweep_disabled" in OVERLAY_OVERRIDABLE_SETTINGS
        assert "todo_sweep_recheck_interval_hours" in OVERLAY_OVERRIDABLE_SETTINGS

    def test_config_parses_knobs(self) -> None:
        import tempfile  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415

        from teatree.config import load_config  # noqa: PLC0415

        path = Path(tempfile.mkdtemp(prefix="todo_cfg_")) / ".teatree.toml"
        path.write_text("[teatree]\ntodo_sweep_disabled = true\ntodo_sweep_recheck_interval_hours = 6\n")
        self.addCleanup(path.unlink, missing_ok=True)
        settings = load_config(path).user
        assert settings.todo_sweep_disabled is True
        assert settings.todo_sweep_recheck_interval_hours == 6


class BuilderTests(TestCase):
    def _backend(self, *, overlay: object, name: str = "t3-acme") -> object:
        return type("B", (), {"overlay": overlay, "name": name})()

    def test_builds_scanner_for_overlay_with_class(self) -> None:
        from teatree.config import UserSettings  # noqa: PLC0415
        from teatree.loop.scanner_factories import _todo_sweep_scanner_for  # noqa: PLC0415

        with patch(
            "teatree.loop.scanner_factories._effective_settings_for_overlay",
            return_value=UserSettings(todo_sweep_recheck_interval_hours=4),
        ):
            scanner = _todo_sweep_scanner_for(self._backend(overlay=_Overlay()))
        assert scanner is not None
        assert scanner.overlay_name == "t3-acme"
        assert scanner.recheck_interval_hours == 4

    def test_returns_none_when_no_overlay_class(self) -> None:
        from teatree.loop.scanner_factories import _todo_sweep_scanner_for  # noqa: PLC0415

        assert _todo_sweep_scanner_for(self._backend(overlay=None)) is None

    def test_returns_none_when_disabled(self) -> None:
        from teatree.config import UserSettings  # noqa: PLC0415
        from teatree.loop.scanner_factories import _todo_sweep_scanner_for  # noqa: PLC0415

        with patch(
            "teatree.loop.scanner_factories._effective_settings_for_overlay",
            return_value=UserSettings(todo_sweep_disabled=True),
        ):
            assert _todo_sweep_scanner_for(self._backend(overlay=_Overlay())) is None

    def test_jobs_for_overlay_backend_wires_scanner(self) -> None:
        from teatree.loop.domain_jobs import _jobs_for_overlay_backend  # noqa: PLC0415
        from teatree.loop.scanners.todo_sweep import TodoSweepScanner  # noqa: PLC0415

        fake = TodoSweepScanner(overlay=_Overlay(), overlay_name="t3-acme")
        backend = type(
            "B",
            (),
            {
                "overlay": _Overlay(),
                "name": "t3-acme",
                "external_db": None,
                "stale_threshold_days": 30,
                "hosts": (),
                "messaging": None,
                "identities": (),
            },
        )()
        with (
            patch("teatree.loop.domain_jobs._todo_sweep_scanner_for", return_value=fake),
            patch("teatree.loop.domain_jobs._architectural_review_scanner_for", return_value=None),
            patch("teatree.loop.domain_jobs._pr_sweep_scanner_for", return_value=None),
            patch("teatree.loop.domain_jobs._pull_main_clone_scanner_for", return_value=None),
            patch("teatree.loop.domain_jobs._codex_review_scanner_for", return_value=None),
            patch("teatree.loop.domain_jobs._slack_broadcasts_scanner_for", return_value=None),
            patch("teatree.loop.domain_jobs._failed_e2e_scanner_for", return_value=None),
            patch("teatree.loop.domain_jobs._user_slack_id_for_overlay", return_value=""),
        ):
            jobs = _jobs_for_overlay_backend(backend)
        assert any(j.scanner is fake and j.overlay == "t3-acme" for j in jobs)
