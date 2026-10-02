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
from contextlib import redirect_stdout
from unittest.mock import patch

from django.test import TestCase

from teatree.cli.doctor.checks_config_drift import (
    DriftedSetting,
    _check_config_rows_shadowing_shipped_defaults,
    drifted_settings,
)
from teatree.core.models import ConfigSetting


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
            "teatree.cli.doctor.checks_config_drift.shipped_defaults_table",
            return_value={"issue_implementer_label": "t3-auto"},
        ):
            out = _run()
        assert "issue_implementer_label" in out
        assert "t3-batch" in out
        assert "t3-auto" in out

    def test_it_returns_without_raising_so_the_exit_code_is_untouched(self) -> None:
        # The invariant that makes it safe to ship: most drifted rows are deliberate, so a
        # finding here must never turn a green doctor run red.
        ConfigSetting.objects.set_value("issue_implementer_label", "t3-batch")
        with patch(
            "teatree.cli.doctor.checks_config_drift.shipped_defaults_table",
            return_value={"issue_implementer_label": "t3-auto"},
        ):
            assert _check_config_rows_shadowing_shipped_defaults() is None

    def test_an_undrifted_store_says_nothing(self) -> None:
        with patch("teatree.cli.doctor.checks_config_drift.shipped_defaults_table", return_value={}):
            assert _run() == ""

    def test_a_crashing_read_degrades_to_a_warn_line(self) -> None:
        # Every sibling advisory is crash-proof: a doctor run must complete even when one
        # probe's read blows up, or one broken check hides every other finding.
        with patch(
            "teatree.cli.doctor.checks_config_drift.shipped_defaults_table",
            side_effect=RuntimeError("db down"),
        ):
            out = _run()
        assert "WARN" in out

    def test_a_drifted_secret_is_reported_with_its_value_masked(self) -> None:
        # The row still has to surface — a secret that drifted is exactly as misleading as any
        # other — but neither the stored nor the shipped value may reach the output.
        ConfigSetting.objects.set_value("banned_terms", ["leaked-term"])
        with patch(
            "teatree.cli.doctor.checks_config_drift.shipped_defaults_table",
            return_value={"banned_terms": ["shipped-term"]},
        ):
            out = _run()
        assert "banned_terms" in out
        assert "leaked-term" not in out
        assert "shipped-term" not in out
