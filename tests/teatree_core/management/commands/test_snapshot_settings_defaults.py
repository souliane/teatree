"""``snapshot_settings_defaults`` — propose the DB→file snapshot, write only once approved.

The command NEVER writes unattended. A bare run renders the proposed diff and records a
:class:`DeferredQuestion` the owner answers through the existing
``t3 teatree questions list|answer`` seam; ``--apply`` writes only when that exact
question is answered ``approve``, and only for the fingerprint of the diff that was
shown. No Slack call is made here — the question body carries the monospace table and
the existing ``DeferredQuestionPosterScanner`` mirrors it.
"""

import io
import tomllib
from pathlib import Path
from unittest import mock

import pytest
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from django_typer.management import TyperCommand

from teatree.config import cold_defaults
from teatree.config.cold_defaults import flatten_settings_table
from teatree.config.defaults_approvals import read_approvals
from teatree.config.defaults_snapshot import ShippedFile, plan_fingerprint, plan_snapshot
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
        self.ledger = tmp_path / "defaults_approvals.toml"
        monkeypatch.setattr(cold_defaults, "DEFAULTS_TOML", self.defaults)
        monkeypatch.setattr(command_module, "APPROVALS_TOML", self.ledger)
        monkeypatch.setattr(command_module, "export_scan_terms", list)

    def _shipped(self) -> dict[str, object]:
        """The fixture file's ``[teatree]`` table in the FLAT namespace the planner works in."""
        return flatten_settings_table(tomllib.loads(self.defaults.read_text(encoding="utf-8"))["teatree"])

    def _run(self, *args: str) -> str:
        out, err = io.StringIO(), io.StringIO()
        call_command("snapshot_settings_defaults", *args, stdout=out, stderr=err)
        return out.getvalue() + err.getvalue()

    def _fingerprint(self) -> str:
        plan = plan_snapshot(
            shipped=ShippedFile(table=self._shipped(), text=self.defaults.read_text(encoding="utf-8")),
            live_global=ConfigSetting.objects.overrides_for_scope(""),
            overlay_scope_rows=[],
            banned_scan=lambda _text: None,
        )
        return plan_fingerprint(plan.changes)


class TestProposeNeverWrites(SnapshotCommandTestCase):
    def test_a_bare_run_records_a_question_and_writes_nothing(self) -> None:
        before = self.defaults.read_text(encoding="utf-8")
        ConfigSetting.objects.set_value(_TUNABLE, 42)

        output = self._run()

        assert self.defaults.read_text(encoding="utf-8") == before
        assert not self.ledger.exists()
        question = DeferredQuestion.objects.get()
        assert question.is_pending
        assert _TUNABLE in question.question
        assert "42" in question.question
        assert "```" in question.question, "the diff is rendered as a monospace fence, never a pipe table"
        assert "|---" not in question.question
        assert str(question.pk) in output

    def test_the_question_carries_the_plan_fingerprint(self) -> None:
        ConfigSetting.objects.set_value(_TUNABLE, 42)
        self._run()
        assert DeferredQuestion.objects.get().options_hash == f"defaults_snapshot:{self._fingerprint()}"

    def test_re_proposing_the_same_diff_does_not_flood_the_queue(self) -> None:
        ConfigSetting.objects.set_value(_TUNABLE, 42)
        self._run()
        self._run()
        assert DeferredQuestion.objects.count() == 1

    def test_nothing_to_change_records_no_question(self) -> None:
        assert "no change" in self._run().lower()
        assert DeferredQuestion.objects.count() == 0

    def test_the_command_help_names_the_owner_approval(self) -> None:
        assert issubclass(Command, TyperCommand)
        assert "owner-approved" in Command.help

    def test_no_slack_call_is_made(self) -> None:
        ConfigSetting.objects.set_value(_TUNABLE, 42)
        with mock.patch("teatree.core.notify.notify_user") as notify:
            self._run()
        notify.assert_not_called()


class TestApplyRequiresTheOwnersApproval(SnapshotCommandTestCase):
    def _approve(self, answer: str = "approve") -> DeferredQuestion:
        question = DeferredQuestion.pending().last()
        assert question is not None
        answered = question.apply_answer(answer, resolved_via=DeferredQuestion.ResolvedVia.LOCAL)
        assert answered is not None
        return answered

    def test_apply_without_any_question_refuses(self) -> None:
        ConfigSetting.objects.set_value(_TUNABLE, 42)
        before = self.defaults.read_text(encoding="utf-8")
        with pytest.raises(SystemExit):
            self._run("--apply")
        assert self.defaults.read_text(encoding="utf-8") == before

    def test_apply_with_an_unanswered_question_refuses(self) -> None:
        ConfigSetting.objects.set_value(_TUNABLE, 42)
        self._run()
        before = self.defaults.read_text(encoding="utf-8")
        with pytest.raises(SystemExit):
            self._run("--apply")
        assert self.defaults.read_text(encoding="utf-8") == before

    def test_apply_with_a_denial_refuses(self) -> None:
        ConfigSetting.objects.set_value(_TUNABLE, 42)
        self._run()
        self._approve("no — keep the shipped value")
        before = self.defaults.read_text(encoding="utf-8")
        with pytest.raises(SystemExit):
            self._run("--apply")
        assert self.defaults.read_text(encoding="utf-8") == before

    def test_an_approval_for_a_different_diff_does_not_authorize_this_one(self) -> None:
        ConfigSetting.objects.set_value(_TUNABLE, 42)
        self._run()
        self._approve()
        # The live box moved again after the owner approved the first rendering.
        ConfigSetting.objects.set_value(_TUNABLE, 37)
        before = self.defaults.read_text(encoding="utf-8")
        with pytest.raises(SystemExit):
            self._run("--apply")
        assert self.defaults.read_text(encoding="utf-8") == before

    def test_an_approved_diff_is_written_with_its_approval_recorded(self) -> None:
        ConfigSetting.objects.set_value(_TUNABLE, 42)
        self._run()
        question = self._approve()

        self._run("--apply")

        assert self._shipped()[_TUNABLE] == 42
        approval = read_approvals(self.ledger)[_TUNABLE]
        assert (approval.value, approval.question_id) == (42, question.pk)

    def test_a_value_back_at_the_in_code_default_drops_its_stale_approval(self) -> None:
        from teatree.config.settings import UserSettings  # noqa: PLC0415 — local to the reverting case

        ConfigSetting.objects.set_value(_TUNABLE, 42)
        self._run()
        self._approve()
        self._run("--apply")
        assert _TUNABLE in read_approvals(self.ledger)

        ConfigSetting.objects.set_value(_TUNABLE, getattr(UserSettings(), _TUNABLE))
        self._run()
        self._approve()
        self._run("--apply")
        assert _TUNABLE not in read_approvals(self.ledger)


class TestPinnedKeysCannotMoveThroughThisPath(SnapshotCommandTestCase):
    def test_a_live_safety_posture_override_never_reaches_the_file(self) -> None:
        ConfigSetting.objects.set_value("on_behalf_post_mode", "immediate")
        assert "no change" in self._run().lower()
        assert DeferredQuestion.objects.count() == 0
        assert self._shipped()["on_behalf_post_mode"] == "draft_or_ask"

    def test_a_live_dark_flag_override_never_reaches_the_file(self) -> None:
        # `outer_loop_enabled` is the still-DARK exemplar; `directive_loop_enabled`
        # graduated to SETTLING in #3895 and is no longer pinned by this path.
        ConfigSetting.objects.set_value("outer_loop_enabled", value=True)
        self._run()
        assert self._shipped()["outer_loop_enabled"] is False

    def test_a_pinned_override_is_reported_as_declined(self) -> None:
        ConfigSetting.objects.set_value("autonomy", "babysit")
        assert "safety-posture" in self._run()


class TestSiblingSeedTablesSurviveAnApply(SnapshotCommandTestCase):
    """An approved write re-renders `[teatree]` alone — it never drops the seed tables."""

    def test_the_written_file_keeps_every_seed_table(self) -> None:
        ConfigSetting.objects.set_value(_TUNABLE, 42)
        before = tomllib.loads(self.defaults.read_text(encoding="utf-8"))
        self._run()
        DeferredQuestion.objects.update(answer_text="approve", answered_at=timezone.now())

        self._run("--apply")

        after = tomllib.loads(self.defaults.read_text(encoding="utf-8"))
        assert flatten_settings_table(after["teatree"])[_TUNABLE] == 42
        for table in ("loops", "modes", "schedules"):
            assert after[table] == before[table]


class TestAnApplyPreservesTheNestedShape(SnapshotCommandTestCase):
    """The writer emits the SAME nested shape it replaced — a snapshot never re-flattens it.

    Without this, the next approved snapshot would rewrite `[teatree]` as one alphabetical
    wall and undo the grouping the owner hand-edits the shipped file through.
    """

    def _teatree_block(self) -> str:
        """The GROUP region — the nested wrappers, before the sub-table settings."""
        text = self.defaults.read_text(encoding="utf-8")
        section = text[text.index("\n[teatree.") :]
        return section[: section.index("\n[teatree.mr_reminder]")]

    def _group_headers(self) -> list[str]:
        return [line for line in self._teatree_block().splitlines() if line.startswith("[teatree.")]

    def _key_order(self) -> tuple[str, ...]:
        return tuple(line.split(" =")[0] for line in self._teatree_block().splitlines() if " = " in line)

    def _approved_apply(self) -> None:
        self._run()
        DeferredQuestion.objects.update(answer_text="approve", answered_at=timezone.now())
        self._run("--apply")

    def test_the_written_block_keeps_the_group_tables(self) -> None:
        before = self._group_headers()
        assert before, "precondition: the shipped file already carries nested group tables"
        ConfigSetting.objects.set_value(_TUNABLE, 42)

        self._approved_apply()

        assert self._group_headers() == before

    def test_the_written_block_keeps_the_group_order(self) -> None:
        ConfigSetting.objects.set_value(_TUNABLE, 42)

        self._approved_apply()

        order = self._key_order()
        assert order == grouped_key_order(order)
        assert order != tuple(sorted(order)), "the apply re-flattened the block to alphabetical"

    def test_the_written_block_moves_only_the_approved_value(self) -> None:
        before = self._shipped()
        ConfigSetting.objects.set_value(_TUNABLE, 42)

        self._approved_apply()

        assert self._shipped() == {**before, _TUNABLE: 42}

    def test_the_sub_tables_stay_below_the_group_tables(self) -> None:
        ConfigSetting.objects.set_value(_TUNABLE, 42)

        self._approved_apply()

        text = self.defaults.read_text(encoding="utf-8")
        assert text.index("\n[teatree.mr_reminder]") > text.rindex(f"\n{_TUNABLE} = ")
        assert "mr_reminder" not in self._key_order()
