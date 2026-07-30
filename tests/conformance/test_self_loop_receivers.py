"""The self-loop guard: no ``post_transition`` receiver ignores a state-preserving replay.

A transition that lists its own target among its sources (``Ticket.mark_reviewed_externally``
re-stamping a moved head SHA) is idempotent in STATE but not in SIDE EFFECTS, so a receiver
keyed on entering a state re-mints its work on every replay. The live gate asserts every
receiver under ``src/teatree`` has decided what a self-loop means; the synthetic lanes prove
the walk is anti-vacuous — RED on a planted target-only receiver, GREEN on the guarded and
the explicitly-marked forms.

The set of senders that can even reach the class is DERIVED from the live FSMs rather than
listed here, so adding a self-loop to a model turns this lane RED on that model's receivers.
"""

from pathlib import Path

from django.apps import apps
from django.db.models import Model
from django_fsm import ANY_STATE, FSMFieldMixin

from teatree.quality.self_loop_receivers import SELF_LOOP_SAFE_PRAGMA, scan_source, scan_tree

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src" / "teatree"

#: Every receiver teatree connects today; the floor keeps a refactor that loses sight of the
#: signal from emptying the walk into a vacuous green.
_RECEIVER_FLOOR = 6


def _admits_self_loop(model: type[Model]) -> bool:
    """True when any FSM transition on *model* can leave the state unchanged."""
    for field in model._meta.get_fields():
        if not isinstance(field, FSMFieldMixin):
            continue
        for transition in field.get_all_transitions(model):
            if transition.source in {transition.target, ANY_STATE}:
                return True
    return False


def _self_looping_senders() -> frozenset[str]:
    return frozenset(model.__name__ for model in apps.get_models() if _admits_self_loop(model))


def _reasons(source: str) -> list[str]:
    return [v.receiver for v in scan_source(source, Path("<test>")).violations]


_TARGET_ONLY = """
def _mint(sender, instance, target, **kwargs):
    if target in _TERMINAL:
        enqueue(instance.pk)

post_transition.connect(_mint, sender=Ticket)
"""

_GUARDED = """
def _mint(sender, instance, source, target, **kwargs):
    if target in _TERMINAL and source != target:
        enqueue(instance.pk)

post_transition.connect(_mint, sender=Ticket)
"""


class TestLiveTreeIsClean:
    def test_every_receiver_decides_what_a_self_loop_means(self) -> None:
        result = scan_tree([_SRC], _self_looping_senders())
        rendered = "\n".join(
            f"  {v.path.relative_to(_REPO_ROOT)}:{v.lineno} {v.receiver} (sender={v.sender or '?'}) — {v.reason}"
            for v in result.violations
        )
        assert not result.violations, f"post_transition receiver(s) blind to a self-loop:\n{rendered}"

    def test_the_walk_sees_every_connected_receiver(self) -> None:
        result = scan_tree([_SRC])
        assert len(result.receivers) >= _RECEIVER_FLOOR
        assert {"_log_ticket_transition", "_enqueue_ticket_transition_task"} <= {r.name for r in result.receivers}


class TestSelfLoopingSendersAreDerived:
    """The exemption is the live FSM's answer, never a hand-kept list."""

    def test_ticket_and_worktree_admit_a_self_loop(self) -> None:
        assert {"Ticket", "Worktree"} <= _self_looping_senders()

    def test_pull_request_has_no_self_loop_so_its_receivers_are_exempt(self) -> None:
        senders = _self_looping_senders()
        assert "PullRequest" not in senders
        assert not scan_source(_TARGET_ONLY.replace("Ticket", "PullRequest"), Path("<x>"), senders).violations


class TestAntiVacuity:
    def test_target_only_receiver_is_flagged(self) -> None:
        assert _reasons(_TARGET_ONLY) == ["_mint"]

    def test_source_target_comparison_clears_it(self) -> None:
        assert _reasons(_GUARDED) == []

    def test_equality_form_of_the_guard_clears_it(self) -> None:
        early_return = _TARGET_ONLY.replace("if target in", "if source == target:\n        return\n    if target in")
        assert _reasons(early_return) == []

    def test_receiver_with_no_target_at_all_is_still_flagged(self) -> None:
        # The class is not only target-keyed: a name-keyed reaction post re-fires too.
        source = """
def _react(instance, name, **kwargs):
    publish(instance, name)

post_transition.connect(_react, sender=Ticket)
"""
        assert _reasons(source) == ["_react"]

    def test_marker_with_a_reason_opts_out(self) -> None:
        source = f"""
def _react(instance, name, **kwargs):
    # {SELF_LOOP_SAFE_PRAGMA} keyed on the transition NAME, whose re-invocation is the request
    publish(instance, name)

post_transition.connect(_react, sender=Ticket)
"""
        assert _reasons(source) == []

    def test_bare_marker_without_a_reason_does_not_silence_it(self) -> None:
        source = f"""
def _react(instance, name, **kwargs):
    # {SELF_LOOP_SAFE_PRAGMA} ok
    publish(instance, name)

post_transition.connect(_react, sender=Ticket)
"""
        assert _reasons(source) == ["_react"]

    def test_unresolvable_receiver_fails_closed(self) -> None:
        assert _reasons("post_transition.connect(imported_elsewhere, sender=Ticket)\n") == ["imported_elsewhere"]

    def test_connect_without_a_sender_is_treated_as_self_looping(self) -> None:
        result = scan_source("def _f(**kwargs):\n    pass\n\npost_transition.connect(_f)\n", Path("<x>"), frozenset())
        assert [v.receiver for v in result.violations] == ["_f"]

    def test_a_different_signal_is_out_of_scope(self) -> None:
        assert _reasons("def _f(**kwargs):\n    pass\n\npost_save.connect(_f, sender=Ticket)\n") == []

    def test_receiver_decorator_form_is_discovered(self) -> None:
        source = """
@receiver(post_transition, sender=Ticket)
def _mint(sender, instance, target, **kwargs):
    enqueue(instance.pk)
"""
        assert _reasons(source) == ["_mint"]

    def test_scan_tree_skips_a_nonexistent_root(self) -> None:
        assert scan_tree([_REPO_ROOT / "does_not_exist"]).receivers == ()
