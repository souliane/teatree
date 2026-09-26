"""``snapshot_settings_defaults`` — a REPORT of live-vs-shipped, which writes nothing.

``[teatree]`` is rendered from the settings declarations, so a write here would be put
back by the next render; the command's job is to say which of this box's global rows
differ and what file they would produce. The shape assertions that used to run against a
written file run against the PLAN instead — the same rendered text, one step earlier.
"""

import io
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
from teatree.core.management.commands import snapshot_settings_defaults as command_module
from teatree.core.management.commands.snapshot_settings_defaults import Command
from teatree.core.models import ConfigSetting
from teatree.core.models.deferred_question import DeferredQuestion

_TUNABLE = "provision_ram_ceiling_percent"


class SnapshotCommandTestCase(TestCase):
    @pytest.fixture(autouse=True)
    def _files(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        source = cold_defaults.DEFAULTS_TOML.read_text(encoding="utf-8")
        self.defaults = tmp_path / "defaults.toml"
        self.defaults.write_text(source, encoding="utf-8")
        monkeypatch.setattr(cold_defaults, "DEFAULTS_TOML", self.defaults)
        monkeypatch.setattr(command_module, "export_scan_terms", list)

    def _shipped(self) -> dict[str, object]:
        """The fixture file's ``[teatree]`` table in the FLAT namespace the planner works in."""
        return flatten_settings_table(tomllib.loads(self.defaults.read_text(encoding="utf-8"))["teatree"])

    def _run(self, *args: str) -> str:
        out, err = io.StringIO(), io.StringIO()
        call_command("snapshot_settings_defaults", *args, stdout=out, stderr=err)
        return out.getvalue() + err.getvalue()

    def _plan(self) -> SnapshotPlan:
        return plan_snapshot(
            shipped=ShippedFile(table=self._shipped(), text=self.defaults.read_text(encoding="utf-8")),
            live_global=ConfigSetting.objects.overrides_for_scope(""),
            overlay_scope_rows=[],
            banned_scan=lambda _text: None,
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

    def test_a_live_dark_flag_override_never_reaches_the_proposal(self) -> None:
        # `outer_loop_enabled` is the still-DARK exemplar; `directive_loop_enabled`
        # graduated to SETTLING in #3895 and is no longer pinned by this path.
        ConfigSetting.objects.set_value("outer_loop_enabled", value=True)
        assert _emitted(self._plan().toml)["outer_loop_enabled"] is False

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
