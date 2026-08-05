"""``manage.py loop_directive_set`` — the reachable, versioned off switch (#4166).

Integration-first: drives the real command via ``call_command`` and asserts the
resolver's own view of the result, because "the slot stops being delivered" is the
behaviour, not "a row was written".
"""

import io

import django.test
import pytest
from django.core.management import call_command

from teatree.core.models import Prompt
from teatree.loop.standing_directives import STANDING_DIRECTIVES, override_prompt_name, resolve_standing_directives


def _run(*args: str, **kwargs: object) -> None:
    call_command("loop_directive_set", *args, stderr=io.StringIO(), **kwargs)


def _resolved_slots() -> list[str]:
    return [directive.slot_id for directive in resolve_standing_directives()]


class TestDisable(django.test.TestCase):
    def test_a_disabled_slot_stops_resolving(self) -> None:
        _run("disable", "standing-pr-board")

        assert "standing-pr-board" not in _resolved_slots()

    def test_disabling_an_owner_edited_slot_snapshots_the_superseded_body(self) -> None:
        Prompt.objects.create(name=override_prompt_name("standing-pr-board"), body="Owner board rule.")

        _run("disable", "standing-pr-board")

        prompt = Prompt.objects.by_name(override_prompt_name("standing-pr-board"))
        assert prompt is not None
        assert [v.body for v in prompt.versions.all()] == ["Owner board rule."]

    def test_disabling_an_already_disabled_slot_churns_no_version(self) -> None:
        _run("disable", "standing-pr-board")
        _run("disable", "standing-pr-board")

        prompt = Prompt.objects.by_name(override_prompt_name("standing-pr-board"))
        assert prompt is not None
        assert prompt.versions.count() == 0

    def test_all_is_the_whole_feature_kill(self) -> None:
        _run("disable", all_slots=True)

        assert _resolved_slots() == []


class TestEnable(django.test.TestCase):
    def test_enable_restores_the_owners_own_text(self) -> None:
        Prompt.objects.create(name=override_prompt_name("standing-pr-board"), body="Owner board rule.")
        _run("disable", "standing-pr-board")

        _run("enable", "standing-pr-board")

        by_slot = {d.slot_id: d.text for d in resolve_standing_directives()}
        assert by_slot["standing-pr-board"] == "Owner board rule."

    def test_enable_with_no_snapshot_falls_back_to_the_compiled_default(self) -> None:
        _run("disable", "standing-golden-rule")

        _run("enable", "standing-golden-rule")

        by_slot = {d.slot_id: d.text for d in resolve_standing_directives()}
        assert by_slot["standing-golden-rule"] == _compiled_text("standing-golden-rule")
        assert Prompt.objects.by_name(override_prompt_name("standing-golden-rule")) is None

    def test_the_all_round_trip_restores_every_slot(self) -> None:
        _run("disable", all_slots=True)

        _run("enable", all_slots=True)

        assert _resolved_slots() == [d.slot_id for d in STANDING_DIRECTIVES]

    def test_enabling_a_slot_that_was_never_disabled_is_a_no_op(self) -> None:
        _run("enable", "standing-todo-consolidate")

        assert "standing-todo-consolidate" in _resolved_slots()

    def test_enable_leaves_a_live_owner_body_alone(self) -> None:
        # ``versions`` holds SUPERSEDED bodies, never the live one, so restoring
        # the newest non-empty version over an already-on slot reverts the owner's
        # latest edit. Reachable from the documented `enable --all` undo.
        prompt = Prompt.objects.create(name=override_prompt_name("standing-pr-board"), body="First board rule.")
        prompt.revise(body="Second board rule.")

        _run("enable", "standing-pr-board")

        by_slot = {d.slot_id: d.text for d in resolve_standing_directives()}
        assert by_slot["standing-pr-board"] == "Second board rule."

    def test_enable_keeps_text_the_owner_authored_after_the_disable(self) -> None:
        # The disable snapshots an EMPTY body as v1, so a body authored afterwards
        # has no non-empty version behind it — the delete branch would destroy it.
        _run("disable", "standing-pr-board")
        prompt = Prompt.objects.by_name(override_prompt_name("standing-pr-board"))
        assert prompt is not None
        prompt.revise(body="Re-authored board rule.")

        _run("enable", "standing-pr-board")

        assert Prompt.objects.by_name(override_prompt_name("standing-pr-board")) is not None
        by_slot = {d.slot_id: d.text for d in resolve_standing_directives()}
        assert by_slot["standing-pr-board"] == "Re-authored board rule."


class TestRefusals(django.test.TestCase):
    def test_an_unknown_slot_exits_non_zero_and_names_the_valid_ids(self) -> None:
        # SystemExit, never typer.Exit: under call_command a typer.Exit is
        # swallowed and a real failure reports success.
        err = io.StringIO()

        with pytest.raises(SystemExit) as exc:
            call_command("loop_directive_set", "disable", "standing-nope", stderr=err)

        assert exc.value.code == 2
        assert "standing-golden-rule" in err.getvalue()

    def test_an_unknown_slot_alongside_all_is_still_refused(self) -> None:
        # --all honoured first would silently disable every slot on a typo, which
        # is the opposite of what the owner asked for.
        err = io.StringIO()

        with pytest.raises(SystemExit) as exc:
            call_command("loop_directive_set", "disable", "standing-nope", "--all", stderr=err)

        assert exc.value.code == 2
        assert "standing-nope" in err.getvalue()
        assert _resolved_slots() == [d.slot_id for d in STANDING_DIRECTIVES]

    def test_naming_no_slot_and_no_all_exits_non_zero(self) -> None:
        err = io.StringIO()

        with pytest.raises(SystemExit) as exc:
            call_command("loop_directive_set", "disable", stderr=err)

        assert exc.value.code == 2


def _compiled_text(slot_id: str) -> str:
    return next(d.default_text for d in STANDING_DIRECTIVES if d.slot_id == slot_id)
