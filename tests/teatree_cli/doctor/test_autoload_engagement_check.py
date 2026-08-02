"""``t3 doctor check``'s autoload-engagement probe.

Reading ``autoload`` back correctly is not the contract the owner cares about;
"the platform skill is loaded on every new session" is. Between the stored
``True`` and that outcome sit the engagement seam, the demand set, the canonical
token it is written as, and the resolver the skill-loading gate consults —
every one of which degrades SILENTLY into a session that looks exactly like one
where autoload was never switched on. These tests pin the probe that turns that
recurring hand-diagnosis into one doctor line.
"""

import contextlib
import io
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

import teatree
from teatree.cli.doctor.checks_cold_hooks import (
    _ENGAGEMENT_PROBE,
    _check_autoload_engages_platform_skill,
    _run_hook_probe,
)

_PROBE = "teatree.cli.doctor.checks_cold_hooks._run_hook_probe"
_SETTINGS = "teatree.config.get_effective_settings"


def _run_check() -> tuple[bool, str]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        verdict = _check_autoload_engages_platform_skill()
    return verdict, buffer.getvalue()


@dataclass(frozen=True)
class _Settings:
    autoload: bool


class TestProbeRunsAgainstTheRealShim(TestCase):
    """Integration: the probe really drives this repo's own hook shim."""

    def test_live_hook_path_reports_an_enforceable_platform_skill(self) -> None:
        repo_root = Path(teatree.__file__).resolve().parents[2]
        with patch.dict("os.environ", {"T3_AUTOLOAD": "1"}):
            parsed = _run_hook_probe(repo_root, _ENGAGEMENT_PROBE.format(plugin_root=str(repo_root)))

        assert parsed is not None, "the hook shim probe did not run at all"
        assert parsed["status"] == "ok", parsed
        # The whole chain: seam -> demand -> canonical token -> resolvable.
        assert parsed["enforceable"], parsed

    def test_missing_shim_is_unaskable_not_a_crash(self) -> None:
        assert _run_hook_probe(Path("/nonexistent-repo-root"), "print('{}')") is None


class TestAutoloadOffIsSilent(TestCase):
    def test_nothing_is_claimed_so_nothing_is_checked(self) -> None:
        with patch(_SETTINGS, return_value=_Settings(autoload=False)), patch(_PROBE) as probe:
            verdict, output = _run_check()

        assert verdict is True
        assert output == ""
        probe.assert_not_called()


class TestSilentlyDegradedEngagementFails(TestCase):
    def test_no_enforceable_demand_is_a_hard_fail(self) -> None:
        # The exact live shape of the bug: autoload stored True, the hook path
        # runs fine, and engages nothing at all.
        with (
            patch(_SETTINGS, return_value=_Settings(autoload=True)),
            patch(_PROBE, return_value={"status": "ok", "demand": [], "enforceable": []}),
        ):
            verdict, output = _run_check()

        assert verdict is False
        assert "FAIL" in output
        # Names the owner-visible symptom, not just an internal value.
        assert "never opted in" in output

    def test_a_demand_that_resolves_to_nothing_enforceable_still_fails(self) -> None:
        # The subtler shape: a demand IS computed but the resolver drops it, so
        # the skill-loading gate can never enforce it.
        with (
            patch(_SETTINGS, return_value=_Settings(autoload=True)),
            patch(_PROBE, return_value={"status": "ok", "demand": ["teatree"], "enforceable": []}),
        ):
            verdict, output = _run_check()

        assert verdict is False
        assert "'teatree'" in output

    def test_a_crashing_probe_is_a_fail_not_a_warn(self) -> None:
        # A probe that RAN and crashed settles the question: the live hook path
        # cannot compute the demand, so no session is engaging anything.
        with (
            patch(_SETTINGS, return_value=_Settings(autoload=True)),
            patch(_PROBE, return_value={"status": "probe_failed", "error": "ImportError: boom"}),
        ):
            verdict, output = _run_check()

        assert verdict is False
        assert "FAIL" in output
        assert "ImportError: boom" in output


class TestUnaskableProbeWarns(TestCase):
    def test_an_undiagnosable_environment_never_turns_the_run_red(self) -> None:
        with patch(_SETTINGS, return_value=_Settings(autoload=True)), patch(_PROBE, return_value=None):
            verdict, output = _run_check()

        assert verdict is True
        assert "WARN" in output
        assert "FAIL" not in output


class TestHealthyEngagementPasses(TestCase):
    def test_an_enforceable_demand_is_silently_ok(self) -> None:
        with (
            patch(_SETTINGS, return_value=_Settings(autoload=True)),
            patch(_PROBE, return_value={"status": "ok", "demand": ["teatree"], "enforceable": ["t3:teatree"]}),
        ):
            verdict, output = _run_check()

        assert verdict is True
        assert output == ""
