"""``t3 doctor check``'s cold-hook settings probe (#3499).

The CLI and the hooks read the same store through DIFFERENT interpreters — `t3` from a
uv-tool venv that can import ``teatree``, the hooks from whatever ``run-hook.sh`` picks
off ``PATH``. A store the CLI reads fine can be totally unreadable to every cold-hook
gate, and nothing surfaced that. These tests pin the probe that now does.
"""

import contextlib
import io
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

import teatree
from teatree.cli.doctor.checks_cold_hooks import (
    HookResolution,
    _check_cold_hook_settings_readable,
    _hook_interpreter_resolution,
)
from teatree.core.models import ConfigSetting

_MODULE = "teatree.cli.doctor.checks_cold_hooks._hook_interpreter_resolution"


def _run_check() -> tuple[bool, str]:
    """Run the check, returning ``(verdict, printed_output)``."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        verdict = _check_cold_hook_settings_readable()
    return verdict, buffer.getvalue()


class TestHookInterpreterProbe(TestCase):
    """Integration: the probe really drives this repo's own hook shim."""

    def test_probe_reaches_the_store_through_the_real_hook_shim(self) -> None:
        repo_root = Path(teatree.__file__).resolve().parents[2]
        resolution = _hook_interpreter_resolution(repo_root)
        assert resolution is not None, "the hook shim probe did not run at all"
        assert resolution.status == "ok", f"hook interpreter cannot read the store: {resolution}"
        assert isinstance(resolution.autoload, bool)

    def test_missing_shim_is_unaskable_not_a_crash(self) -> None:
        assert _hook_interpreter_resolution(Path("/nonexistent-repo-root")) is None


class TestUnreadableStoreFails(TestCase):
    def test_unreadable_store_is_a_hard_fail_naming_the_blast_radius(self) -> None:
        with patch(_MODULE, return_value=HookResolution(status="unreadable")):
            verdict, output = _run_check()
        assert verdict is False
        assert "FAIL" in output
        # The operator must learn this is not autoload-only: every gate is affected.
        assert "cold-hook gate" in output
        assert "inert" in output


class TestCliHookDisagreementFails(TestCase):
    def test_hook_and_cli_disagreeing_on_autoload_is_a_hard_fail(self) -> None:
        """The exact #3499 shape: store says True, the hooks resolve False."""
        ConfigSetting.objects.set_value("autoload", value=True, scope="")
        with patch(_MODULE, return_value=HookResolution(status="ok", autoload=False)):
            verdict, output = _run_check()
        assert verdict is False
        assert "FAIL" in output
        assert "disagree" in output

    def test_agreement_is_silently_ok(self) -> None:
        ConfigSetting.objects.set_value("autoload", value=True, scope="")
        with patch(_MODULE, return_value=HookResolution(status="ok", autoload=True)):
            verdict, output = _run_check()
        assert verdict is True
        assert output.strip() == ""


class TestUndiagnosableEnvironmentWarnsOnly(TestCase):
    """An environment the probe cannot interrogate must not turn the run red."""

    def test_unaskable_probe_warns_and_passes(self) -> None:
        with patch(_MODULE, return_value=None):
            verdict, output = _run_check()
        assert verdict is True
        assert "WARN" in output

    def test_probe_crash_inside_the_hook_interpreter_warns_and_passes(self) -> None:
        with patch(_MODULE, return_value=HookResolution(status="probe_failed", error="ImportError: boom")):
            verdict, output = _run_check()
        assert verdict is True
        assert "WARN" in output
        assert "ImportError: boom" in output
