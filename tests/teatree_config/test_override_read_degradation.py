"""A failed ``ConfigSetting`` override read must not resolve like an absent override (#3873).

The defect: :func:`teatree.config.resolution._load_global_rows` caught every read
exception and returned ``{}`` — the SAME value the success-with-no-rows path returns. So
"there is no override" and "I could not determine whether there is an override" reached
every call site as one answer, and the safety gates resolved to their SHIPPED defaults
(``autonomy = full``, ``mode = auto``) rather than to whatever the operator configured.

Each case below pins one half of the distinction. The paired foils matter as much as the
assertions: a resolver that degraded EVERYTHING would satisfy the fail-closed cases while
being useless, so every fail-closed case has an absent-override twin that must still
resolve to the shipped default.
"""

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from django.core.exceptions import AppRegistryNotReady, SynchronousOnlyOperation
from django.db.utils import OperationalError
from django.test import TestCase

from teatree.config import get_effective_settings
from teatree.config.enums import Autonomy, Mode, OnBehalfPostMode
from teatree.config.override_read_health import (
    MARKER_FILENAME,
    MAX_RECORDED_CALLERS,
    SAFETY_FAIL_CLOSED_STORED_VALUES,
    ConfigOverrideReadError,
    clear_degraded_read,
    degraded_read_report,
    fallback_marker_path,
    marker_path,
    marker_paths,
    record_degraded_read,
)
from teatree.config.provenance import ValueSource, resolve_settings
from teatree.config.resolution import fail_closed_overrides, read_setting_layers
from teatree.core.models import ConfigSetting
from teatree.paths import ControlDb, data_dir_root

_GLOBAL = "global"


class _RaisingOverrides:
    """A ``ConfigSetting.objects`` stand-in whose scope reads raise *exc* the first *times* calls."""

    def __init__(self, exc: BaseException, *, times: int = 10**6, rows: dict[str, Any] | None = None) -> None:
        self._exc = exc
        self._remaining = times
        self._rows = rows or {}
        self.calls = 0

    def overrides_for_scope(self, scope: str) -> dict[str, Any]:
        self.calls += 1
        if self._remaining > 0:
            self._remaining -= 1
            raise self._exc
        return dict(self._rows)

    def exclude(self, **_kwargs: object) -> "_RaisingOverrides":
        return self

    def values_list(self, *_fields: str) -> list[tuple[str, str, Any]]:
        self.calls += 1
        if self._remaining > 0:
            self._remaining -= 1
            raise self._exc
        return []


def _with_failing_reads(exc: BaseException, **kwargs: object) -> Any:
    """Patch the app-registry model lookup so every ``ConfigSetting`` scope read raises *exc*."""
    manager = _RaisingOverrides(exc, **kwargs)
    model = mock.Mock(objects=manager)
    patcher = mock.patch("django.apps.apps.get_model", return_value=model)
    return patcher, manager


class TestAFailedReadIsNotAnAbsentOverride(TestCase):
    def test_a_runtime_read_fault_marks_the_scope_degraded(self) -> None:
        patcher, _ = _with_failing_reads(OperationalError("database is locked"))
        with patcher:
            layers = read_setting_layers("")
        assert _GLOBAL in layers.degraded_scopes

    def test_a_clean_read_with_no_rows_is_not_degraded(self) -> None:
        # The foil for the case above: an EMPTY table must stay indistinguishable from
        # today's behaviour, or every install would resolve as if its config were broken.
        assert read_setting_layers("").degraded_scopes == frozenset()

    def test_a_bootstrap_state_is_not_reported_as_degraded(self) -> None:
        # Django not yet set up is a genuine no-op, not a fault — degrading here would
        # fail-close every cold-start read.
        patcher, _ = _with_failing_reads(AppRegistryNotReady("apps aren't loaded yet"))
        with patcher:
            layers = read_setting_layers("")
        assert layers.degraded_scopes == frozenset()


class TestSafetyGatesFailClosedRatherThanToAShippedDefault(TestCase):
    def test_autonomy_fails_closed_when_the_override_read_fails(self) -> None:
        # The shipped default is FULL. A read fault must NOT resolve to it: the operator
        # may have stored `babysit`, and a gate cannot tell the two apart today.
        patcher, _ = _with_failing_reads(OperationalError("database is locked"))
        with patcher:
            settings = get_effective_settings("t3-teatree")
        assert settings.autonomy is Autonomy.BABYSIT

    def test_autonomy_still_resolves_to_the_shipped_default_when_the_read_succeeds(self) -> None:
        # The foil. Without it, a resolver that always fail-closed would pass the case above.
        assert get_effective_settings("t3-teatree").autonomy is Autonomy.FULL

    def test_the_merge_approval_gate_fails_closed(self) -> None:
        patcher, _ = _with_failing_reads(OperationalError("database is locked"))
        with patcher:
            settings = get_effective_settings("t3-teatree")
        assert settings.require_human_approval_to_merge is True
        assert settings.require_human_approval_to_answer is True

    def test_a_stored_restrictive_tier_is_not_replaced_by_the_permissive_shipped_one(self) -> None:
        # The sharpest shape, and the reason the shipped default is the wrong fallback:
        # `autonomy` ships as FULL, so an operator who deliberately stored BABYSIT has
        # their restraint UPGRADED to full autonomy by a read that merely failed. The
        # stored value and the failure are indistinguishable at the call site today.
        ConfigSetting.objects.set_value("autonomy", "babysit")
        assert get_effective_settings("t3-teatree").autonomy is Autonomy.BABYSIT
        patcher, _ = _with_failing_reads(OperationalError("database is locked"))
        with patcher:
            settings = get_effective_settings("t3-teatree")
        assert settings.autonomy is Autonomy.BABYSIT

    def test_mode_and_on_behalf_posting_fail_closed(self) -> None:
        patcher, _ = _with_failing_reads(OperationalError("database is locked"))
        with patcher:
            settings = get_effective_settings("t3-teatree")
        assert settings.mode is Mode.INTERACTIVE
        assert settings.on_behalf_post_mode is OnBehalfPostMode.DRAFT_OR_ASK

    def test_an_env_override_still_wins_over_the_fail_closed_value(self) -> None:
        # `T3_*` is process state the failed DB read cannot have affected, so it is a
        # readable operator intent and must not be overridden by the degradation.
        patcher, _ = _with_failing_reads(OperationalError("database is locked"))
        with patcher, mock.patch.dict(os.environ, {"T3_MODE": "auto"}):
            settings = get_effective_settings()
        assert settings.mode is Mode.AUTO

    def test_every_fail_closed_value_names_a_real_settings_field(self) -> None:
        settings = get_effective_settings("t3-teatree")
        for key in SAFETY_FAIL_CLOSED_STORED_VALUES:
            assert hasattr(settings, key), f"{key!r} is not a UserSettings field"


class TestATransientLockIsRetriedRatherThanDegradedStraightAway(TestCase):
    def test_a_read_that_succeeds_on_retry_resolves_the_stored_override(self) -> None:
        # Reproduces the MECHANISM (a transient SQLite lock), not the load: one contended
        # read raises, the next succeeds. Today the first exception ends the read.
        patcher, manager = _with_failing_reads(
            OperationalError("database is locked"),
            times=1,
            rows={"require_human_approval_to_merge": False},
        )
        with patcher:
            layers = read_setting_layers("")
        assert manager.calls > 1, "the read was not retried"
        assert layers.degraded_scopes == frozenset()
        assert layers.global_db["require_human_approval_to_merge"] is False

    def test_a_persistently_failing_read_degrades_rather_than_retrying_forever(self) -> None:
        patcher, manager = _with_failing_reads(OperationalError("database is locked"))
        with patcher:
            layers = read_setting_layers("")
        assert _GLOBAL in layers.degraded_scopes
        assert manager.calls < 10, "the retry budget is unbounded"


class TestADeterministicFaultIsNotSpentOnTheContentionBudget(TestCase):
    """#3980: the retry exists for CONTENTION; a deterministic fault must skip it entirely.

    ``SynchronousOnlyOperation`` is a property of WHERE the read was called from, so it fails
    identically on every attempt. Retrying it adds the full backoff to a failure that was
    certain, and makes a programming error read like a flaky one.
    """

    def test_a_synchronous_only_operation_is_attempted_exactly_once(self) -> None:
        patcher, manager = _with_failing_reads(SynchronousOnlyOperation("You cannot call this from an async context"))
        with patcher:
            layers = read_setting_layers("")
        assert manager.calls == 1, "a deterministic fault was retried under the contention budget"
        assert _GLOBAL in layers.degraded_scopes

    def test_a_contended_read_still_spends_the_budget(self) -> None:
        # The foil: narrowing the retry must not disarm it for the fault it was built for.
        patcher, manager = _with_failing_reads(OperationalError("database is locked"))
        with patcher:
            read_setting_layers("")
        assert manager.calls > 1, "the contention retry was disarmed"


class TestTheRecordedFailureNamesTheCallingContext(TestCase):
    """#3980: the traceback holds only the ORM frames, which is the same for every fault.

    The one fact that makes the failure actionable — which call site read the config tier — is
    ABOVE this module in the stack, so it has to be captured deliberately and recorded where an
    operator reads it, not only in a log line nobody tails.
    """

    def test_the_marker_records_the_frame_that_asked_for_the_read(self) -> None:
        patcher, _ = _with_failing_reads(SynchronousOnlyOperation("You cannot call this from an async context"))
        with mock.patch("teatree.config.override_read_health.marker_path", return_value=self._tmp_marker()), patcher:
            read_setting_layers("")
            report = degraded_read_report()
        assert report is not None
        assert any(__name__.rsplit(".", 1)[-1] in caller for caller in report.callers), report.callers

    def test_the_loud_log_names_the_caller_and_says_it_was_not_retried(self) -> None:
        patcher, _ = _with_failing_reads(SynchronousOnlyOperation("You cannot call this from an async context"))
        with (
            mock.patch("teatree.config.override_read_health.marker_path", return_value=self._tmp_marker()),
            patcher,
            self.assertLogs("teatree.config", level="ERROR") as logs,
        ):
            read_setting_layers("")
        message = "\n".join(logs.output)
        assert __name__.rsplit(".", 1)[-1] in message
        assert "not retried" in message.lower()

    def test_the_recorded_callers_are_bounded(self) -> None:
        # A degraded read repeats at whatever rate its caller runs at, so an unbounded record
        # would grow the marker file for as long as the fault lasts.
        tmp = self._tmp_marker()
        with mock.patch("teatree.config.override_read_health.marker_path", return_value=tmp):
            for index in range(MAX_RECORDED_CALLERS + 4):
                record_degraded_read("global", caller=f"caller_{index}.py:1 in f")
            report = degraded_read_report()
        assert report is not None
        assert len(report.callers) == MAX_RECORDED_CALLERS
        assert report.occurrences == MAX_RECORDED_CALLERS + 4

    def _tmp_marker(self) -> Path:
        tmp = Path(tempfile.mkdtemp()) / "config-read-degraded.json"
        self.addCleanup(lambda: tmp.unlink(missing_ok=True))
        return tmp


class TestTheDivergenceIsObservable(TestCase):
    def test_provenance_names_the_unresolved_tier_instead_of_crediting_a_shipped_one(self) -> None:
        patcher, _ = _with_failing_reads(OperationalError("database is locked"))
        with patcher:
            resolved = resolve_settings(["autonomy"])["autonomy"]
        assert resolved.source is ValueSource.UNRESOLVED

    def test_provenance_still_credits_the_shipped_file_on_a_clean_read(self) -> None:
        assert resolve_settings(["autonomy"])["autonomy"].source is ValueSource.SHIPPED_FILE

    def test_a_degraded_read_is_recorded_outside_the_database_it_could_not_read(self) -> None:
        # The record cannot live in the DB — the DB is the thing that failed.
        with mock.patch("teatree.config.override_read_health.marker_path") as marker:
            marker.return_value = self._tmp_marker()
            record_degraded_read("global")
            report = degraded_read_report()
        assert report is not None
        assert report.scopes == ("global",)
        assert report.occurrences == 1

    def test_no_report_when_nothing_degraded(self) -> None:
        with mock.patch("teatree.config.override_read_health.marker_path") as marker:
            marker.return_value = self._tmp_marker()
            assert degraded_read_report() is None

    def _tmp_marker(self) -> Path:
        tmp = Path(tempfile.mkdtemp()) / "config-read-degraded.json"
        self.addCleanup(lambda: tmp.unlink(missing_ok=True))
        return tmp


class TestTheFailClosedTableIsAppliedRatherThanAssumed(TestCase):
    def test_nothing_is_forced_when_no_scope_degraded(self) -> None:
        # Inert on every healthy read — the resolution stays byte-identical to before.
        assert fail_closed_overrides(frozenset(), supplied_by_env=set()) == {}

    def test_a_degraded_scope_forces_every_safety_key(self) -> None:
        forced = fail_closed_overrides(frozenset({"global"}), supplied_by_env=set())
        assert set(forced) == set(SAFETY_FAIL_CLOSED_STORED_VALUES)
        # Coerced through the SAME registry parsers a stored row goes through, so a
        # fail-closed value can never be a type the resolver would reject.
        assert forced["autonomy"] is Autonomy.BABYSIT
        assert forced["mode"] is Mode.INTERACTIVE

    def test_a_key_supplied_by_env_is_left_alone(self) -> None:
        forced = fail_closed_overrides(frozenset({"global"}), supplied_by_env={"mode"})
        assert "mode" not in forced
        assert "autonomy" in forced


class TestAnExportRefusesRatherThanPersistingAnUnverifiedAbsence(TestCase):
    def test_a_persisted_walk_raises_while_the_tier_is_degraded(self) -> None:
        # A file export writes what it believes the stored tiers hold. Doing that from a
        # tier it could not read would record an absence it never verified — turning a
        # transient read fault into permanent, silent config loss.
        patcher, _ = _with_failing_reads(OperationalError("database is locked"))
        with patcher, pytest.raises(ConfigOverrideReadError):
            resolve_settings(["autonomy"], persisted_only=True)

    def test_the_dashboard_walk_does_not_raise(self) -> None:
        # The foil: the read-only view RENDERS the degradation (that is the point of
        # surfacing it) instead of refusing to render at all.
        patcher, _ = _with_failing_reads(OperationalError("database is locked"))
        with patcher:
            resolved = resolve_settings(["autonomy"], persisted_only=False)
        assert resolved["autonomy"].source is ValueSource.UNRESOLVED

    def test_a_healthy_export_still_walks(self) -> None:
        assert resolve_settings(["autonomy"], persisted_only=True)["autonomy"].key == "autonomy"


class TestTheMarkerCanBeAcknowledged(TestCase):
    def test_clearing_drops_a_live_record(self) -> None:
        tmp = self._tmp_marker()
        with mock.patch("teatree.config.override_read_health.marker_path", return_value=tmp):
            record_degraded_read("global")
            assert degraded_read_report() is not None
            clear_degraded_read()
            assert degraded_read_report() is None

    def test_clearing_an_absent_marker_is_not_an_error(self) -> None:
        tmp = self._tmp_marker()
        with mock.patch("teatree.config.override_read_health.marker_path", return_value=tmp):
            clear_degraded_read()  # must not raise

    def test_the_marker_sits_beside_the_primary_control_db(self) -> None:
        # Beside the DB, never inside it: the store that failed is the one place a record
        # of the failure is guaranteed not to reach.
        assert marker_path().parent == ControlDb(os.environ).primary().parent

    def _tmp_marker(self) -> Path:
        tmp = Path(tempfile.mkdtemp()) / "config-read-degraded.json"
        self.addCleanup(lambda: tmp.unlink(missing_ok=True))
        return tmp


class TestTheMarkerIsRecordedWhereTheFaultWasObserved(TestCase):
    """The record has to land in the venue that SAW the fault, not only in the canonical one (#4041).

    The canonical marker sits inside the container's control-DB volume. A HOST process hits
    the very read failure this records, then cannot create ``/var/lib/teatree`` to write it
    down — so ``record_degraded_read`` fell into its own ``except OSError`` and logged that
    the fault "is visible only in this log". A health marker that cannot be written where
    the fault happens cannot do its job, and the degradation stayed invisible by
    construction while every consumer resolved against a shipped default.

    The unwritable canonical dir here is one whose PARENT is a regular file, so ``mkdir``
    raises for root as well — a permission-bit foil would go vacuous under a root test run.
    """

    def _unwritable_canonical(self) -> Path:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        (root / "blocking-file").write_text("not a directory")
        return root / "blocking-file" / "control-db" / MARKER_FILENAME

    def _writable_fallback(self) -> Path:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        return root / MARKER_FILENAME

    def test_the_fallback_resolves_into_this_venues_own_data_dir(self) -> None:
        # The fallback is only useful if it lands where the venue that observed the fault
        # can write AND where its operator already looks — the same root the host
        # projection is published into. A fallback pointing back inside the control-DB
        # volume would satisfy every other case here while fixing nothing.
        assert fallback_marker_path() == data_dir_root() / MARKER_FILENAME
        assert fallback_marker_path() != marker_path()

    def test_the_marker_is_written_when_the_canonical_path_is_unwritable(self) -> None:
        canonical, fallback = self._unwritable_canonical(), self._writable_fallback()
        with (
            mock.patch("teatree.config.override_read_health.marker_path", return_value=canonical),
            mock.patch("teatree.config.override_read_health.fallback_marker_path", return_value=fallback),
        ):
            record_degraded_read("global", caller="hook.py:1 in f")
            report = degraded_read_report()
        assert fallback.is_file(), "the fault was observed here and recorded nowhere"
        assert not canonical.exists()
        assert report is not None
        assert report.scopes == ("global",)
        assert report.path == fallback, "the operator must be told the file that exists"

    def test_recording_a_failure_emits_no_traceback(self) -> None:
        # C: this runs under the statusline and `t3 loop status`, whose output must stay
        # quiet. Exhausting every candidate is one WARNING line naming the paths tried.
        canonical = self._unwritable_canonical()
        with (
            mock.patch("teatree.config.override_read_health.marker_path", return_value=canonical),
            mock.patch("teatree.config.override_read_health.fallback_marker_path", return_value=canonical),
            self.assertLogs("teatree.config", level="WARNING") as logs,
        ):
            record_degraded_read("global")
        assert len(logs.records) == 1, logs.output
        assert logs.records[0].exc_info is None, "a handled OSError dumped its frames into the bar"
        assert str(canonical) in logs.output[0]

    def test_a_writable_canonical_venue_keeps_exactly_one_marker(self) -> None:
        # The foil: offering the per-user path unconditionally would let a stale host
        # marker outvote a healthy container record — the same defect one layer up.
        canonical, fallback = self._writable_fallback(), self._writable_fallback()
        with (
            mock.patch("teatree.config.override_read_health.marker_path", return_value=canonical),
            mock.patch("teatree.config.override_read_health.fallback_marker_path", return_value=fallback),
        ):
            record_degraded_read("global")
            assert marker_paths() == (canonical,)
        assert canonical.is_file()
        assert not fallback.exists()

    def test_clearing_drops_the_fallback_record_too(self) -> None:
        canonical, fallback = self._unwritable_canonical(), self._writable_fallback()
        with (
            mock.patch("teatree.config.override_read_health.marker_path", return_value=canonical),
            mock.patch("teatree.config.override_read_health.fallback_marker_path", return_value=fallback),
        ):
            record_degraded_read("global")
            assert degraded_read_report() is not None
            clear_degraded_read()
            assert degraded_read_report() is None
        assert not fallback.exists()
