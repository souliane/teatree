"""The overlay CLI's hardcoded DJANGO_GROUPS must expose every ticket subcommand.

`t3 <overlay> ticket <sub>` dispatch is driven by the explicit
``DJANGO_GROUPS`` table in ``teatree.cli.overlay``. A subcommand absent from
that table is unreachable via the overlay CLI even though the core
``ticket`` management command defines it — exactly the regression this guards.
"""

from teatree.cli.overlay import DJANGO_GROUPS
from teatree.core.management.commands.e2e import Command as E2eCommand
from teatree.core.management.commands.honesty import Command as HonestyCommand
from teatree.core.management.commands.learnings import Command as LearningsCommand
from teatree.core.management.commands.lifecycle import Command as LifecycleCommand


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


def _availability_subcommands() -> set[str]:
    return {name for name, _desc in DJANGO_GROUPS["availability"].subcommands}


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


def test_availability_group_exposes_autonomous_away() -> None:
    # #2544: the management command grew an `autonomous-away` subcommand, but
    # without a DJANGO_GROUPS bridge entry `t3 <overlay> availability
    # autonomous-away` returned "No such command" even though the feature
    # (and its docs) shipped — exactly the class of regression this guards.
    assert "autonomous-away" in _availability_subcommands()


def test_learnings_group_exposes_show_add_edit() -> None:
    assert {"show", "add", "edit"} <= _learnings_subcommands()


def test_learnings_group_dispatches_to_core() -> None:
    # ``learnings`` lives in ``teatree.core.management.commands``; it must
    # route via ``managepy_core`` (python -m teatree), not the overlay manage.py.
    assert DJANGO_GROUPS["learnings"].dispatches_to_core("show") is True


def test_learnings_subcommands_map_to_real_command_methods() -> None:
    for name in _learnings_subcommands():
        assert hasattr(LearningsCommand, name.replace("-", "_")), name
