"""The sweep's CI decision applies the SAME required-checks floor the keystone applies.

``classify_required_rollup`` reads a determinate-EMPTY required set as "nothing to
satisfy" → ``green``, so only the floor check tells a genuinely gate-less repo from one
whose branch protection was removed. That check lived solely in the keystone's
``_github_required_checks_verdict``, so the sweep called a PR with zero enforced checks
mergeable while the chokepoint refused the very same PR as ``failed`` — the solo path
re-attempted it every tick and surfaced only the opaque
``solo_overlay_gh_fallback_failed``.
"""

from unittest.mock import patch

import django.test
from django.core.management import call_command

from teatree.loop.scanners.pr_sweep_decision import classify_sweep_ci
from teatree.types import RawAPIDict

_FLOOR_SEAM = "teatree.core.merge.ci_rollup._expected_required_contexts_floor"


def _green(name: str) -> RawAPIDict:
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": "COMPLETED",
        "conclusion": "SUCCESS",
        "startedAt": "2026-08-05T10:00:00Z",
        "completedAt": "2026-08-05T10:05:00Z",
    }


def _classify(rollup: list[RawAPIDict], required: set[str] | None) -> tuple[str | None, bool, set[str]]:
    return classify_sweep_ci(rollup, required, main_uv_audit_red=lambda: False)


class TestEmptyRequiredSetIsPutThroughTheFloor(django.test.TestCase):
    def test_no_required_checks_and_no_floor_is_still_green(self) -> None:
        """A genuinely gate-less repo keeps merging — the floor only bites when configured."""
        assert _classify([_green("eval")], set()) == (None, False, set())

    def test_no_required_checks_under_a_configured_floor_is_refused(self) -> None:
        call_command("config_setting", "set", "expected_required_contexts", '["test (3.13)"]')

        skip_reason, fallback, failing = _classify([_green("test (3.13)")], set())

        assert skip_reason == "required_checks_missing_floor"
        assert fallback is False
        assert failing == set()

    def test_an_unreadable_floor_is_refused_rather_than_read_as_no_floor(self) -> None:
        """Indeterminate is not "no gate" — the sweep fails closed exactly as the keystone does."""
        with patch(_FLOOR_SEAM, return_value=None):
            skip_reason, _fallback, _failing = _classify([_green("test (3.13)")], set())

        assert skip_reason == "required_checks_missing_floor"

    def test_a_present_required_set_is_unaffected_by_the_floor(self) -> None:
        call_command("config_setting", "set", "expected_required_contexts", '["test (3.13)"]')

        assert _classify([_green("test (3.13)")], {"test (3.13)"}) == (None, False, set())

    def test_an_indeterminate_required_lookup_keeps_its_own_skip(self) -> None:
        assert _classify([_green("test (3.13)")], None) == ("required_checks_indeterminate", False, set())
