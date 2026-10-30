"""``snapshot_settings_defaults`` — a REPORT of live-vs-shipped, which writes nothing.

``[teatree]`` is rendered from the settings declarations, so a write here would be put
back by the next render; the command's job is to say which of this box's global rows
differ and what file they would produce. The shape assertions that used to run against a
written file run against the PLAN instead — the same rendered text, one step earlier.
"""

import io
import json
import tomllib
from pathlib import Path
from unittest import mock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django_typer.management import TyperCommand

from teatree.config import cold_defaults
from teatree.config.cold_defaults import flatten_settings_table
from teatree.config.defaults_snapshot import ShippedFile, SnapshotPlan, plan_snapshot
from teatree.config.setting_groups import grouped_key_order
from teatree.core.management.commands.snapshot_settings_defaults import Command
from teatree.core.models import ConfigSetting
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.hooks.banned_term_registry import load_registry
from teatree.hooks.term_match import line_matches
from tests._send_gate import TEST_TERM_REGISTRY, TEST_TERM_REGISTRY_JSON

_TUNABLE = "provision_slow_threshold_seconds"
_LEAK = TEST_TERM_REGISTRY["leak"][0]
_OV = TEST_TERM_REGISTRY["overlay"][0]
_SCAN = tuple(term for terms in TEST_TERM_REGISTRY.values() for term in terms)
#: A runtime-built non-ASCII placeholder — no real word, never a configured term.
_NON_ASCII_TERM = "qx" + "\u00fc" + "mbel-ledger"
#: A DEFAULT key the planner scans rather than declining it as conservative first.
_SCANNED_CARRIER = "colleague_repo_url_pattern"


class SnapshotCommandTestCase(TestCase):
    @pytest.fixture(autouse=True)
    def _files(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.monkeypatch = monkeypatch
        source = cold_defaults.DEFAULTS_TOML.read_text(encoding="utf-8")
        self.defaults = tmp_path / "defaults.toml"
        self.defaults.write_text(source, encoding="utf-8")
        monkeypatch.setattr(cold_defaults, "DEFAULTS_TOML", self.defaults)
        monkeypatch.setenv("TEATREE_TERM_REGISTRY", TEST_TERM_REGISTRY_JSON)

    def _shipped(self) -> dict[str, object]:
        """The fixture file's ``[teatree]`` table in the FLAT namespace the planner works in."""
        return flatten_settings_table(tomllib.loads(self.defaults.read_text(encoding="utf-8"))["teatree"])

    def _run(self, *args: str) -> str:
        out, err = io.StringIO(), io.StringIO()
        call_command("snapshot_settings_defaults", *args, stdout=out, stderr=err)
        return out.getvalue() + err.getvalue()

    def _streams(self) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        try:
            call_command("snapshot_settings_defaults", stdout=out, stderr=err)
        except SystemExit as exc:
            return int(exc.code or 0), out.getvalue(), err.getvalue()
        return 0, out.getvalue(), err.getvalue()

    def _set(self, key: str, value: object, *flags: str) -> None:
        call_command("config_setting", "set", key, json.dumps(value), *flags, stdout=io.StringIO())

    def _plan(self) -> SnapshotPlan:
        return plan_snapshot(
            shipped=ShippedFile(table=self._shipped(), text=self.defaults.read_text(encoding="utf-8")),
            live_global=ConfigSetting.objects.overrides_for_scope(""),
            overlay_scope_rows=[],
            banned_scan=lambda _key, _value: False,
        )


class TestTheReportWritesNothing(SnapshotCommandTestCase):
    def test_a_run_leaves_the_file_and_the_question_queue_untouched(self) -> None:
        before = self.defaults.read_text(encoding="utf-8")
        ConfigSetting.objects.set_value(_TUNABLE, 42)

        self._run()

        assert self.defaults.read_text(encoding="utf-8") == before
        assert DeferredQuestion.objects.count() == 0

    def test_the_report_names_the_key_its_live_value_and_where_to_change_it(self) -> None:
        ConfigSetting.objects.set_value(_TUNABLE, 42)

        output = self._run()

        assert _TUNABLE in output
        assert "42" in output
        assert "declaration" in output

    def test_the_report_carries_the_proposed_file_as_a_diff(self) -> None:
        ConfigSetting.objects.set_value(_TUNABLE, 42)

        output = self._run()

        assert f"+{_TUNABLE} = 42 " in output
        assert "--- shipped" in output

    def test_nothing_to_change_reports_no_change(self) -> None:
        assert "no change" in self._run().lower()

    def test_the_command_help_says_it_writes_nothing(self) -> None:
        assert issubclass(Command, TyperCommand)
        assert "writes nothing" in Command.help

    def test_no_slack_call_is_made(self) -> None:
        ConfigSetting.objects.set_value(_TUNABLE, 42)
        with mock.patch("teatree.core.notify.notify_user") as notify:
            self._run()
        notify.assert_not_called()

    def test_there_is_no_write_arm_left_to_invoke(self) -> None:
        with pytest.raises(CommandError, match="--apply"):
            self._run("--apply")


class TestPinnedKeysCannotMoveThroughThisPath(SnapshotCommandTestCase):
    def test_a_live_safety_posture_override_never_reaches_the_proposal(self) -> None:
        ConfigSetting.objects.set_value("require_human_approval_to_merge", value=False)
        assert "no change" in self._run().lower()
        assert self._shipped()["require_human_approval_to_merge"] is True

    def test_a_live_send_proxy_allowlist_override_never_reaches_the_proposal(self) -> None:
        ConfigSetting.objects.set_value("send_proxy_allowlist", ["C123"])
        assert _emitted(self._plan().toml)["send_proxy_allowlist"] == []

    def test_a_pinned_override_is_reported_as_declined(self) -> None:
        ConfigSetting.objects.set_value("autonomy", "babysit")
        assert "safety-posture" in self._run()


class TestTheProposalKeepsTheShippedShape(SnapshotCommandTestCase):
    """The proposed text re-renders `[teatree]` alone, in the nested shape it replaced.

    A flat or seed-table-dropping proposal would be an unusable diff, and the same renderer
    writes the real file — so these are the shape guards, one step before the write that
    no longer exists.
    """

    def _teatree_block(self, text: str) -> str:
        section = text[text.index("\n[teatree.") :]
        return section[: section.index("\n[teatree.mr_reminder]")]

    def _group_headers(self, text: str) -> list[str]:
        return [line for line in self._teatree_block(text).splitlines() if line.startswith("[teatree.")]

    def _key_order(self, text: str) -> tuple[str, ...]:
        return tuple(line.split(" =")[0] for line in self._teatree_block(text).splitlines() if " = " in line)

    def test_the_proposal_keeps_every_seed_table(self) -> None:
        before = tomllib.loads(self.defaults.read_text(encoding="utf-8"))
        ConfigSetting.objects.set_value(_TUNABLE, 42)

        after = tomllib.loads(self._plan().toml)

        assert flatten_settings_table(after["teatree"])[_TUNABLE] == 42
        for table in ("loops", "modes", "schedules"):
            assert after[table] == before[table]

    def test_the_proposal_keeps_the_group_tables(self) -> None:
        before = self._group_headers(self.defaults.read_text(encoding="utf-8"))
        assert before, "precondition: the shipped file already carries nested group tables"
        ConfigSetting.objects.set_value(_TUNABLE, 42)

        assert self._group_headers(self._plan().toml) == before

    def test_the_proposal_keeps_the_group_order(self) -> None:
        ConfigSetting.objects.set_value(_TUNABLE, 42)

        order = self._key_order(self._plan().toml)

        assert order == grouped_key_order(order)
        assert order != tuple(sorted(order)), "the proposal re-flattened the block to alphabetical"

    def test_the_proposal_moves_only_the_live_value(self) -> None:
        before = self._shipped()
        ConfigSetting.objects.set_value(_TUNABLE, 42)

        assert _emitted(self._plan().toml) == {**before, _TUNABLE: 42}

    def test_the_sub_tables_stay_below_the_group_tables(self) -> None:
        ConfigSetting.objects.set_value(_TUNABLE, 42)

        text = self._plan().toml

        assert text.index("\n[teatree.mr_reminder]") > text.rindex(f"\n{_TUNABLE} = ")


def _emitted(text: str) -> dict[str, object]:
    return flatten_settings_table(tomllib.loads(text)["teatree"])


class TestABannedTermIsNeverPrinted(SnapshotCommandTestCase):
    def test_a_carrier_row_is_declined_by_class_not_by_the_term(self) -> None:
        self._set(_SCANNED_CARRIER, f"{_LEAK}-desk/.*")
        code, out, err = self._streams()
        assert code == 0
        assert f"{_SCANNED_CARRIER}: banned-term" in err
        assert not line_matches(out, _SCAN)
        assert not line_matches(err, _SCAN)

    def test_a_workflow_key_carrying_a_term_is_declined_by_its_own_class_first(self) -> None:
        self._set("issue_implementer_label", f"{_LEAK} desk")
        code, out, err = self._streams()
        assert code == 0
        assert "issue_implementer_label: workflow-engagement" in err
        assert not line_matches(out, _SCAN)
        assert not line_matches(err, _SCAN)

    def test_a_non_ascii_term_is_declined_too(self) -> None:
        self.monkeypatch.setenv("TEATREE_TERM_REGISTRY", json.dumps({"leak": [_NON_ASCII_TERM]}))
        self._set(_SCANNED_CARRIER, f"{_NON_ASCII_TERM}-desk/.*")
        code, out, err = self._streams()
        assert code == 0
        assert f"{_SCANNED_CARRIER}: banned-term" in err
        assert not line_matches(out, (_NON_ASCII_TERM,))
        assert not line_matches(err, (_NON_ASCII_TERM,))

    def test_a_scope_name_carrying_a_term_is_listed_without_it(self) -> None:
        self._set("merge_wip", 4, "--overlay", _OV)
        code, out, err = self._streams()
        assert code == 0
        assert "[under a name that carries a banned term] merge_wip" in err
        assert not line_matches(out, _SCAN)
        assert not line_matches(err, _SCAN)

    def test_a_store_with_no_term_is_refused_with_the_registry_named(self) -> None:
        self.monkeypatch.delenv("TEATREE_TERM_REGISTRY")
        assert load_registry() is None
        code, out, err = self._streams()
        assert code == 2
        assert "banned_term_registry" in err
        assert "proposed changes" not in out + err
