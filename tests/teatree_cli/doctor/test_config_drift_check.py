"""``_check_config_rows_shadowing_shipped_defaults`` — the drifted-row advisory (#4074).

A stored ``ConfigSetting`` row silently shadows a shipped default forever. ``config_setting
get`` reports ``[source: db, global]`` — accurate, and it does not say that the default moved
underneath. The instance that motivated the finding (`issue_implementer_label` stored as the
retired `t3-batch` while the shipped default had become `t3-auto`) cost an hour and three wrong
conclusions in a row.

Most such rows are deliberate operator intent, so this is an ADVISORY listing and never a
failure: it must not gate the exit code, and it must not crash a doctor run. A drifted row is
currently indistinguishable from an intended one — that is the whole finding.
"""

import io
import subprocess
import sys
from contextlib import redirect_stdout
from unittest.mock import patch

from django.db import OperationalError
from django.test import TestCase

from teatree.cli.doctor.checks_config_drift import (
    DriftedSetting,
    _check_config_rows_shadowing_shipped_defaults,
    drifted_settings,
)
from teatree.core.models import ConfigSetting, ConfigSettingChange


def test_doctor_module_imports_before_django_setup() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import teatree.cli.doctor.checks_config_drift"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _run() -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        _check_config_rows_shadowing_shipped_defaults()
    return buf.getvalue()


class TheDriftPredicateTestCase(TestCase):
    """The pure half: which stored rows diverge from the shipped floor."""

    def test_a_row_that_differs_from_the_shipped_default_is_listed(self) -> None:
        found = drifted_settings(
            stored=[("issue_implementer_label", "", "t3-batch")],
            shipped={"issue_implementer_label": "t3-auto"},
        )
        assert found == (DriftedSetting("issue_implementer_label", "", "t3-batch", "t3-auto"),)

    def test_a_row_equal_to_the_shipped_default_is_not_listed(self) -> None:
        # The common case by far — a row written before the value became the default, or one
        # the operator set to the same thing. Nothing to report.
        found = drifted_settings(stored=[("mode", "", "auto")], shipped={"mode": "auto"})
        assert found == ()

    def test_a_key_absent_from_the_shipped_file_is_skipped(self) -> None:
        # There is no shipped VALUE to diverge from, so any claim of drift would be invented.
        # Personal/Secret keys are absent by construction, so this is not an edge case.
        found = drifted_settings(stored=[("workspace_dir", "", "/somewhere")], shipped={})
        assert found == ()

    def test_each_scope_is_judged_on_its_own_row(self) -> None:
        # An overlay row and the global row are separate overrides of the same shipped floor.
        found = drifted_settings(
            stored=[("mode", "", "auto"), ("mode", "demo", "interactive")],
            shipped={"mode": "auto"},
        )
        assert found == (DriftedSetting("mode", "demo", "interactive", "auto"),)


class TheAdvisoryNeverFailsTheRunTestCase(TestCase):
    """The doctor half: it reports, and it never gates or crashes."""

    def test_a_drifted_row_is_named_with_both_values(self) -> None:
        ConfigSetting.objects.set_value("issue_implementer_label", "t3-batch")
        with patch(
            "teatree.cli.doctor.checks_config_drift.declared_defaults",
            return_value={"issue_implementer_label": "t3-auto"},
        ):
            out = _run()
        assert "issue_implementer_label" in out
        assert "t3-batch" in out
        assert "t3-auto" in out

    def test_latest_audit_writer_and_above_maximum_are_reported(self) -> None:
        ConfigSetting.objects.create(key="admission_write_concurrency_per_core", value=3, written_by="old-writer")
        ConfigSettingChange.objects.create(
            key="admission_write_concurrency_per_core",
            scope="",
            action="set",
            old_value=1,
            new_value=3,
            written_by="cold-writer",
        )
        with patch(
            "teatree.cli.doctor.checks_config_drift.declared_defaults",
            return_value={"admission_write_concurrency_per_core": 1},
        ):
            out = _run()
        assert "by cold-writer" in out
        assert "above maximum 2" in out

    def test_it_returns_without_raising_so_the_exit_code_is_untouched(self) -> None:
        # The invariant that makes it safe to ship: most drifted rows are deliberate, so a
        # finding here must never turn a green doctor run red.
        ConfigSetting.objects.set_value("issue_implementer_label", "t3-batch")
        with patch(
            "teatree.cli.doctor.checks_config_drift.declared_defaults",
            return_value={"issue_implementer_label": "t3-auto"},
        ):
            assert _check_config_rows_shadowing_shipped_defaults() is None

    def test_an_undrifted_store_says_nothing(self) -> None:
        with patch("teatree.cli.doctor.checks_config_drift.declared_defaults", return_value={}):
            assert _run() == ""

    def test_a_crashing_read_degrades_to_a_warn_line(self) -> None:
        # Every sibling advisory is crash-proof: a doctor run must complete even when one
        # probe's read blows up, or one broken check hides every other finding.
        with patch(
            "teatree.cli.doctor.checks_config_drift.declared_defaults",
            side_effect=RuntimeError("db down"),
        ):
            out = _run()
        assert "WARN" in out

    def test_missing_audit_table_keeps_drift_findings(self) -> None:
        ConfigSetting.objects.set_value("issue_implementer_label", "t3-batch")

        def unavailable(*args: object, **kwargs: object) -> None:
            message = "no audit table"
            raise OperationalError(message)

        with (
            patch(
                "teatree.cli.doctor.checks_config_drift.declared_defaults",
                return_value={"issue_implementer_label": "t3-auto"},
            ),
            patch("teatree.core.models.ConfigSettingChange.objects.filter", side_effect=unavailable),
        ):
            out = _run()
        assert "issue_implementer_label" in out
        assert "no audit table" in out

    def test_a_drifted_secret_is_reported_with_its_value_masked(self) -> None:
        # The row still has to surface — a secret that drifted is exactly as misleading as any
        # other — but neither the stored nor the shipped value may reach the output.
        ConfigSetting.objects.set_value("banned_term_registry", {"leak": ["leaked-term"], "prose_collider": []})
        with patch(
            "teatree.cli.doctor.checks_config_drift.declared_defaults",
            return_value={"banned_term_registry": {"leak": ["shipped-term"], "prose_collider": []}},
        ):
            out = _run()
        assert "banned_term_registry" in out
        assert "leaked-term" not in out
        assert "shipped-term" not in out

    def test_a_below_floor_override_shows_writer_age_and_previous_value(self) -> None:
        ConfigSetting.objects.create(key="agent_max_turns", value=9, written_by="review-loop")
        ConfigSettingChange.objects.create(
            key="agent_max_turns", scope="", action="set", old_value=1000, new_value=9, written_by="review-loop"
        )
        with patch("teatree.cli.doctor.checks_config_drift.declared_defaults", return_value={"agent_max_turns": 3000}):
            out = _run()
        assert "WARN" in out
        assert "agent_max_turns" in out
        assert "1000" in out
        assert "review-loop" in out
        assert "ago" in out
        assert "below minimum" in out

    def test_an_ignored_overlay_row_is_listed(self) -> None:
        ConfigSetting.objects.create(key="loop_cadence_seconds", scope="demo", value=600)
        with patch(
            "teatree.cli.doctor.checks_config_drift.declared_defaults", return_value={"loop_cadence_seconds": 720}
        ):
            out = _run()
        assert "loop_cadence_seconds" in out
        assert "ignored by every reader" in out
