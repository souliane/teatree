"""The overlay CLI's hardcoded DJANGO_GROUPS must expose every ticket subcommand.

`t3 <overlay> ticket <sub>` dispatch is driven by the explicit
``DJANGO_GROUPS`` table in ``teatree.cli.overlay``. A subcommand absent from
that table is unreachable via the overlay CLI even though the core
``ticket`` management command defines it — exactly the regression this guards.
"""

import typer

from teatree.cli.loop.preset import register as register_preset
from teatree.cli.overlay import DJANGO_GROUPS
from teatree.core.management.commands.cycle_time import Command as CycleTimeCommand
from teatree.core.management.commands.e2e import Command as E2eCommand
from teatree.core.management.commands.honesty import Command as HonestyCommand
from teatree.core.management.commands.learnings import Command as LearningsCommand
from teatree.core.management.commands.lifecycle import Command as LifecycleCommand
from teatree.core.management.commands.loop_preset import Command as LoopPresetCommand


def _ticket_subcommands() -> set[str]:
    return {name for name, _desc in DJANGO_GROUPS["ticket"].subcommands}


def _honesty_subcommands() -> set[str]:
    return {name for name, _desc in DJANGO_GROUPS["honesty"].subcommands}


def _lifecycle_subcommands() -> set[str]:
    return {name for name, _desc in DJANGO_GROUPS["lifecycle"].subcommands}


def _e2e_subcommands() -> set[str]:
    return {name for name, _desc in DJANGO_GROUPS["e2e"].subcommands}


def _pr_subcommands() -> set[str]:
    return {name for name, _desc in DJANGO_GROUPS["pr"].subcommands}


def _learnings_subcommands() -> set[str]:
    return {name for name, _desc in DJANGO_GROUPS["learnings"].subcommands}


def test_ticket_group_exposes_comment() -> None:
    assert "comment" in _ticket_subcommands()


def test_ticket_group_exposes_known_subcommands() -> None:
    assert {"transition", "list", "sync-completions", "comment"} <= _ticket_subcommands()


def test_lifecycle_group_exposes_record_review_skill_run() -> None:
    assert "record-review-skill-run" in _lifecycle_subcommands()


def test_lifecycle_group_exposes_record_review_context() -> None:
    assert "record-review-context" in _lifecycle_subcommands()


def test_lifecycle_subcommands_map_to_real_command_methods() -> None:
    for name in _lifecycle_subcommands():
        assert hasattr(LifecycleCommand, name.replace("-", "_")), name


def test_e2e_group_exposes_deprecated_post_evidence_alias() -> None:
    # The Django management command defines a hidden deprecated alias
    # ``post-evidence``; without a bridge entry in DJANGO_GROUPS the alias
    # is unreachable via ``t3 <overlay> e2e post-evidence``.
    assert "post-evidence" in _e2e_subcommands()


def test_e2e_group_exposes_retract_evidence() -> None:
    # ``retract-evidence`` is defined on the e2e management command but was
    # absent from DJANGO_GROUPS, so ``t3 <overlay> e2e retract-evidence`` was
    # unreachable from the installed CLI even though the command exists — the
    # class of regression this table guards.
    assert "retract-evidence" in _e2e_subcommands()


def test_e2e_group_exposes_tracked_manifest() -> None:
    # #3092: ``tracked-manifest`` prints the authored half of a manifest so a
    # private test repo can commit a byte-stable file; without a DJANGO_GROUPS
    # bridge entry it would be unreachable from the installed CLI.
    assert "tracked-manifest" in _e2e_subcommands()


def test_e2e_subcommands_map_to_real_command_methods() -> None:
    for name in _e2e_subcommands():
        assert hasattr(E2eCommand, name.replace("-", "_")), name


def test_pr_group_exposes_deprecated_post_evidence_alias() -> None:
    # Same as above for the ``pr`` group.
    assert "post-evidence" in _pr_subcommands()


def test_honesty_group_exposes_escalate() -> None:
    # ``skills/rules/SKILL.md`` § "Escalate Honesty-Critical Verification"
    # tells the agent to run ``t3 <overlay> honesty escalate``. The Django
    # management command exists, but without a DJANGO_GROUPS bridge entry the
    # overlay CLI returned "No such command 'honesty'" — the rule referenced a
    # CLI that did not resolve. This pins the bridge so the rule stays runnable.
    assert "escalate" in _honesty_subcommands()


def test_honesty_group_dispatches_to_core() -> None:
    # The honesty command lives in ``teatree.core.management.commands``; it must
    # route via ``managepy_core`` (python -m teatree), not the overlay manage.py.
    assert DJANGO_GROUPS["honesty"].dispatches_to_core("escalate") is True


def test_honesty_subcommands_map_to_real_command_methods() -> None:
    for name in _honesty_subcommands():
        assert hasattr(HonestyCommand, name.replace("-", "_")), name


def _cycle_time_subcommands() -> set[str]:
    return {name for name, _desc in DJANGO_GROUPS["cycle_time"].subcommands}


def test_cycle_time_group_exposes_both_read_verbs() -> None:
    # #4480: the measurement modules computed spans/timelines/distributions that no
    # command could read. Without a bridge entry the overlay CLI has no cycle-time
    # front door at all and the data stays unreadable.
    assert _cycle_time_subcommands() == {"ticket", "distribution"}


def test_cycle_time_group_dispatches_to_core() -> None:
    # It reads the teatree-core control DB, so it must route via ``managepy_core``
    # rather than an overlay manage.py whose settings never register the command.
    assert DJANGO_GROUPS["cycle_time"].dispatches_to_core("distribution") is True


def test_cycle_time_group_name_matches_the_django_command_module() -> None:
    # ``_bridge_subcommand`` passes the group name verbatim to ``managepy_core``, so a
    # hyphenated group would dispatch a command name Django cannot resolve.
    assert "cycle_time" in DJANGO_GROUPS
    assert "cycle-time" not in DJANGO_GROUPS


def test_cycle_time_subcommands_map_to_real_command_methods() -> None:
    for name in _cycle_time_subcommands():
        assert hasattr(CycleTimeCommand, name.replace("-", "_")), name


def test_loop_preset_group_exposes_every_management_subcommand() -> None:
    # #2544 caught a management subcommand that shipped with no CLI bridge entry, so
    # `t3 <overlay> availability autonomous-away` answered "No such command" while the
    # feature and its docs said otherwise. #3826 retired that group and folded the mode
    # surface into `t3 loop preset`, so the same guard now watches the bridge that
    # replaced it: every verb the management command defines must be reachable.
    preset_app = typer.Typer()
    register_preset(preset_app)
    bridged = {
        command.name for group in preset_app.registered_groups for command in group.typer_instance.registered_commands
    }
    defined = {command.name for command in LoopPresetCommand.typer_app.registered_commands}
    assert {"use", "auto", "show"} <= defined, f"the mode verbs vanished from the command: {sorted(defined)}"
    assert defined <= bridged, f"unbridged loop_preset subcommand(s): {sorted(defined - bridged)}"


def test_learnings_group_exposes_show_add_edit() -> None:
    assert {"show", "add", "edit"} <= _learnings_subcommands()


def test_learnings_group_dispatches_to_core() -> None:
    # ``learnings`` lives in ``teatree.core.management.commands``; it must
    # route via ``managepy_core`` (python -m teatree), not the overlay manage.py.
    assert DJANGO_GROUPS["learnings"].dispatches_to_core("show") is True


def test_learnings_subcommands_map_to_real_command_methods() -> None:
    for name in _learnings_subcommands():
        assert hasattr(LearningsCommand, name.replace("-", "_")), name
