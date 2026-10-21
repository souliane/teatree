"""Harness-neutral standing directives — the layer-1 contract (#4166 Phase 1).

The three directives, their cadences, their per-slot scope, which of them drive
work, the ``Prompt``-row override, the ``{slot_id, cadence_seconds, text, scope}``
read surface every harness consumes, and the publication the Django-free hooks read. Nothing here knows about
slash commands, hooks, or session markers — that is the adapter's layer, pinned
separately in ``tests/teatree_hooks/test_standing_directives_delivery.py``.
"""

import logging
import re
import threading
from unittest import mock

import pytest
from django.db.utils import OperationalError
from django.test import TestCase

from teatree import standing_directives_cache
from teatree.core.mode_resolution import ResolvedMode
from teatree.core.models import Mode, Prompt
from teatree.loop.standing_directives import (
    DISPATCH_LOOP,
    MAX_DIRECTIVE_CHARS,
    SCOPE_ATTENDED,
    SCOPE_ATTENDED_SINGLETON,
    STANDING_DIRECTIVES,
    ResolvedDirective,
    StandingDirective,
    _dispatch_masked,
    compiled_directives,
    golden_rule_cadence_seconds,
    override_prompt_name,
    pr_board_cadence_seconds,
    publish,
    resolve_standing_directives,
    todo_consolidate_cadence_seconds,
)
from teatree.standing_directives_cache import StandingDirectivePayload

_MODE_RESOLVER = "teatree.core.mode_resolution.resolve_active_mode"
_EDIT_WINDOW_SECONDS = 1.0


def _text(slot_id: str) -> str:
    return next(d.default_text for d in STANDING_DIRECTIVES if d.slot_id == slot_id)


# ── the harness-vocabulary predicate (minor 5) ───────────────────────
#
# The old guard was a five-substring denylist that let bare `/loop` and
# `/t3:interactive` through on a trailing space and named no session marker. A
# predicate over the SHAPE of a slash token makes that whole class impossible,
# and the control corpus below is what proves the predicate can go red.

_SLASH_SHAPED_TOKEN = re.compile(r"(?<!\S)/[A-Za-z0-9][\w:.\-]*")

_HARNESS_TOKENS = (
    ".teatree-active",
    ".t3-engaged",
    "teatree-active",
    "t3-engaged",
    "directives-pending",
    "loop-pending",
    "session marker",
    "pretooluse",
    "userpromptsubmit",
    "sessionstart",
    "stop hook",
    "hook",
    "additionalcontext",
    "claude",
    "anthropic",
    "cursor",
    "copilot",
    "codex",
    "slash command",
)

#: Mutations that MUST be caught. The first is the reviewer's own — it slipped
#: past the shipped denylist, which is why the guard is a predicate now.
_HARNESS_VOCABULARY_MUTATIONS = (
    " Set the .teatree-active session marker.",
    " per the /t3:interactive workflow.",
    " register a /loop",
    " the UserPromptSubmit hook injects this.",
    " ask Claude to do it.",
)


def harness_vocabulary_violations(text: str) -> list[str]:
    """Every harness-specific token in *text* — empty means layer-1 neutral."""
    lowered = text.lower()
    found = {match.group(0) for match in _SLASH_SHAPED_TOKEN.finditer(text)}
    found.update(token for token in _HARNESS_TOKENS if token in lowered)
    return sorted(found)


class TestTheThreeSlots:
    def test_exactly_three_slots_in_order(self) -> None:
        assert [d.slot_id for d in STANDING_DIRECTIVES] == [
            "standing-golden-rule",
            "standing-todo-consolidate",
            "standing-pr-board",
        ]

    def test_golden_rule_covers_planning_and_the_orchestrate_only_boundary(self) -> None:
        text = _text("standing-golden-rule")
        assert "PLAN" in text
        for agent in ("t3:coder", "t3:debugger", "t3:tester", "t3:e2e"):
            assert agent in text
        assert "t3:planner" in text
        assert "skip-planning" in text
        assert "NOT a plan" in text
        # The second coupled failure: the orchestrator doing the work itself.
        # Assert the BEHAVIOUR clause, never a pointer to a harness-specific
        # skill — the module claims to carry no such vocabulary.
        assert "never implements itself" in text
        assert "delegate every implementation" in text

    def test_todo_directive_is_durable_state_first_with_a_conditional_rescan(self) -> None:
        text = _text("standing-todo-consolidate")
        assert "durable state FIRST" in text
        assert "ONLY if" in text
        assert "transcript" in text
        assert "outstanding user requests" in text

    def test_todo_directive_carries_the_drain_half(self) -> None:
        # Capture alone lets the list only grow: an OPEN task must also be
        # checked against durable state and closed when already satisfied.
        text = _text("standing-todo-consolidate")
        assert "OPEN task" in text
        assert "already satisfies it" in text
        assert "CLOSE it" in text
        assert "FALSE OPEN" in text
        # The closing invariant: the list must drain, not only grow, across a session.
        assert "DRAIN" in text
        assert "not only grow" in text

    def test_pr_board_directive_names_the_keystone_and_its_guards(self) -> None:
        text = _text("standing-pr-board")
        assert "ticket clear" in text
        assert "ticket merge" in text
        assert "LIVE head" in text
        assert "never a raw forge-CLI merge" in text
        assert "never merge over a hold" in text
        assert "maker ≠ checker" in text

    def test_every_default_text_is_within_the_context_cost_cap(self) -> None:
        for directive in STANDING_DIRECTIVES:
            assert len(directive.default_text) <= MAX_DIRECTIVE_CHARS, directive.slot_id

    def test_the_slot_table_is_the_scope_and_work_contract(self) -> None:
        # The golden rule costs nothing and reaches a deliberately idle session; the
        # other two send the session to work, so the mode brake below may drop them,
        # and the board is one board per host, so it reaches one session only.
        by_slot = {d.slot_id: (d.scope, d.drives_work) for d in STANDING_DIRECTIVES}

        assert by_slot == {
            "standing-golden-rule": (SCOPE_ATTENDED, False),
            "standing-todo-consolidate": (SCOPE_ATTENDED, True),
            "standing-pr-board": (SCOPE_ATTENDED_SINGLETON, True),
        }


class TestHarnessVocabularyIsAbsent:
    """Minor 5: the neutrality guard, and the control corpus proving it can fail."""

    @pytest.mark.parametrize("directive", STANDING_DIRECTIVES, ids=lambda d: d.slot_id)
    def test_a_real_directive_text_is_clean(self, directive: StandingDirective) -> None:
        assert harness_vocabulary_violations(directive.default_text) == []

    @pytest.mark.parametrize("mutation", _HARNESS_VOCABULARY_MUTATIONS)
    def test_control_a_harness_token_appended_to_a_real_text_is_caught(self, mutation: str) -> None:
        # CONTROL — each of these passed the shipped substring denylist. If any
        # returns clean, the guard is vacuous and its green means nothing.
        mutated = _text("standing-golden-rule") + mutation

        assert harness_vocabulary_violations(mutated) != []


class TestCadences:
    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("T3_GOLDEN_RULE_CADENCE", "T3_TODO_CONSOLIDATE_CADENCE", "T3_PR_BOARD_CADENCE"):
            monkeypatch.delenv(name, raising=False)
        assert golden_rule_cadence_seconds() == 300
        assert todo_consolidate_cadence_seconds() == 1800
        assert pr_board_cadence_seconds() == 600

    def test_env_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_GOLDEN_RULE_CADENCE", "900")
        monkeypatch.setenv("T3_TODO_CONSOLIDATE_CADENCE", "3600")
        monkeypatch.setenv("T3_PR_BOARD_CADENCE", "1200")
        assert golden_rule_cadence_seconds() == 900
        assert todo_consolidate_cadence_seconds() == 3600
        assert pr_board_cadence_seconds() == 1200

    def test_floors_clamp_a_too_tight_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_GOLDEN_RULE_CADENCE", "1")
        monkeypatch.setenv("T3_TODO_CONSOLIDATE_CADENCE", "1")
        monkeypatch.setenv("T3_PR_BOARD_CADENCE", "1")
        assert golden_rule_cadence_seconds() == 60
        assert todo_consolidate_cadence_seconds() == 600
        assert pr_board_cadence_seconds() == 300

    def test_the_old_floors_are_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The floor is the real bound on how often a slot is re-delivered. A
        # configuration AT the old, tighter floors must now be clamped up, not honoured.
        monkeypatch.setenv("T3_TODO_CONSOLIDATE_CADENCE", "300")
        monkeypatch.setenv("T3_PR_BOARD_CADENCE", "120")

        assert todo_consolidate_cadence_seconds() == 600
        assert pr_board_cadence_seconds() == 300

    @pytest.mark.parametrize("raw", ["", "   ", "not-a-number"])
    def test_garbage_override_degrades_to_the_default(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv("T3_GOLDEN_RULE_CADENCE", raw)
        assert golden_rule_cadence_seconds() == 300


class TestResolveStandingDirectives(TestCase):
    """The read surface: compiled defaults, ``Prompt``-row override, fail-open."""

    def test_resolves_all_three_from_the_compiled_defaults(self) -> None:
        resolved = resolve_standing_directives()

        assert [r.slot_id for r in resolved] == [d.slot_id for d in STANDING_DIRECTIVES]
        assert [r.text for r in resolved] == [d.default_text for d in STANDING_DIRECTIVES]
        assert [r.cadence_seconds for r in resolved] == [300, 1800, 600]
        assert [r.scope for r in resolved] == [SCOPE_ATTENDED, SCOPE_ATTENDED, SCOPE_ATTENDED_SINGLETON]

    def test_the_compiled_directives_are_the_defaults_without_any_store(self) -> None:
        with mock.patch("teatree.loop.standing_directives._override_texts") as store:
            compiled = compiled_directives()

        store.assert_not_called()
        assert [(r.slot_id, r.cadence_seconds, r.text, r.scope) for r in compiled] == [
            (d.slot_id, d.cadence_seconds(), d.default_text, d.scope) for d in STANDING_DIRECTIVES
        ]

    def test_prompt_row_override_wins_over_the_compiled_default(self) -> None:
        Prompt.objects.create(name=override_prompt_name("standing-pr-board"), body="Owner-edited board rule.")

        by_slot = {r.slot_id: r.text for r in resolve_standing_directives()}

        assert by_slot["standing-pr-board"] == "Owner-edited board rule."
        assert by_slot["standing-golden-rule"] == _text("standing-golden-rule")

    def test_an_empty_override_switches_that_slot_off(self) -> None:
        Prompt.objects.create(name=override_prompt_name("standing-todo-consolidate"), body="   ")

        assert [r.slot_id for r in resolve_standing_directives()] == [
            "standing-golden-rule",
            "standing-pr-board",
        ]

    def test_an_over_cap_override_falls_back_to_the_compiled_default(self) -> None:
        Prompt.objects.create(
            name=override_prompt_name("standing-golden-rule"),
            body="x" * (MAX_DIRECTIVE_CHARS + 1),
        )

        by_slot = {r.slot_id: r.text for r in resolve_standing_directives()}

        assert by_slot["standing-golden-rule"] == _text("standing-golden-rule")

    def test_an_unreachable_override_store_still_yields_the_compiled_defaults(self) -> None:
        with mock.patch(
            "teatree.loop.standing_directives._override_texts",
            side_effect=RuntimeError("no database"),
        ):
            resolved = resolve_standing_directives()

        assert [r.text for r in resolved] == [d.default_text for d in STANDING_DIRECTIVES]

    def test_as_dict_is_the_documented_four_key_contract(self) -> None:
        payload = resolve_standing_directives()[0].as_dict()

        # The declared TypedDict IS the contract, so the emitted payload's keys
        # must equal its annotations — not merely a hand-copied literal set.
        assert set(payload) == set(StandingDirectivePayload.__annotations__)
        assert set(payload) == {"slot_id", "cadence_seconds", "text", "scope"}
        assert payload["slot_id"] == "standing-golden-rule"
        assert payload["scope"] == "attended"


class TestPublish(TestCase):
    """The resolution is published for the Django-free hooks, which never read the store."""

    def test_publishes_exactly_what_resolves(self) -> None:
        Prompt.objects.create(name=override_prompt_name("standing-pr-board"), body="Owner board rule.")

        assert publish() is True
        assert standing_directives_cache.read() == [d.as_dict() for d in resolve_standing_directives()]

    def test_a_switched_off_slot_is_not_published(self) -> None:
        Prompt.objects.create(name=override_prompt_name("standing-todo-consolidate"), body="")

        publish()

        published = standing_directives_cache.read()
        assert published is not None
        assert [d["slot_id"] for d in published] == ["standing-golden-rule", "standing-pr-board"]

    def test_every_slot_off_publishes_an_empty_list_not_nothing(self) -> None:
        for directive in STANDING_DIRECTIVES:
            Prompt.objects.create(name=override_prompt_name(directive.slot_id), body="")

        publish()

        assert standing_directives_cache.read() == []

    def test_an_unchanged_resolution_is_not_republished(self) -> None:
        publish()

        assert publish() is False

    def test_a_resolution_read_before_an_owner_edit_never_lands_after_the_edit_is_published(self) -> None:
        before_the_edit = compiled_directives()
        after_the_edit = [d for d in before_the_edit if d.slot_id != "standing-todo-consolidate"]
        chain_resolved = threading.Event()
        owner_published = threading.Event()

        def resolution() -> list[ResolvedDirective]:
            if threading.current_thread().name != "publish-chain":
                return after_the_edit
            chain_resolved.set()
            # The chain's read stays in flight across the owner's edit, for as long as the edit lets it.
            owner_published.wait(timeout=_EDIT_WINDOW_SECONDS)
            return before_the_edit

        def owner_disables() -> None:
            publish()
            owner_published.set()

        with mock.patch(f"{publish.__module__}.resolve_standing_directives", side_effect=resolution):
            chain = threading.Thread(target=publish, name="publish-chain")
            chain.start()
            assert chain_resolved.wait(timeout=30)
            owner = threading.Thread(target=owner_disables)
            owner.start()
            chain.join()
            owner.join()

        assert standing_directives_cache.read() == [d.as_dict() for d in after_the_edit]


_DEGRADED_STORE = "no such table: core_modeoverride"


class TestTheSelfPumpBrake(TestCase):
    """A mode masking the dispatch loop off is one where nothing should be driving work."""

    @staticmethod
    def _resolved(*, pauses: bool, source: str = "override") -> ResolvedMode:
        entries = {DISPATCH_LOOP: False} if pauses else {DISPATCH_LOOP: True}
        mode = Mode(name="off" if pauses else "present", entries=entries)
        return ResolvedMode(mode=mode, source=source, until=None, reason="test")

    def test_a_masked_dispatch_drops_the_work_driving_slots_and_keeps_the_golden_rule(self) -> None:
        with mock.patch(_MODE_RESOLVER, return_value=self._resolved(pauses=True)):
            resolved = resolve_standing_directives()

        assert [r.slot_id for r in resolved] == ["standing-golden-rule"]

    def test_a_masked_dispatch_is_published_too(self) -> None:
        with mock.patch(_MODE_RESOLVER, return_value=self._resolved(pauses=True)):
            publish()

        published = standing_directives_cache.read()
        assert published is not None
        assert [d["slot_id"] for d in published] == ["standing-golden-rule"]

    def test_a_mode_that_does_not_pause_the_pump_delivers_everything(self) -> None:
        with mock.patch(_MODE_RESOLVER, return_value=self._resolved(pauses=False, source="schedule")):
            braked = _dispatch_masked()
            resolved = resolve_standing_directives()

        assert braked is False
        assert len(resolved) == len(STANDING_DIRECTIVES)

    def test_the_brake_reads_the_merged_mode_never_the_preset_layer(self) -> None:
        # #4196: the override/schedule layer stops at ``None`` when neither governs, so
        # braking on it would ignore the configured default mode entirely.
        with mock.patch("teatree.loop.preset_resolution._resolve_active_preset", return_value=None):
            braked = _dispatch_masked()
            resolved = resolve_standing_directives()

        assert braked is False
        assert len(resolved) == len(STANDING_DIRECTIVES)

    def test_no_resolved_work_driving_slot_never_reads_the_mode(self) -> None:
        for directive in STANDING_DIRECTIVES:
            if directive.drives_work:
                Prompt.objects.create(name=override_prompt_name(directive.slot_id), body="")

        with mock.patch(_MODE_RESOLVER) as resolver:
            resolved = resolve_standing_directives()

        assert [r.slot_id for r in resolved] == ["standing-golden-rule"]
        resolver.assert_not_called()

    def test_a_degraded_store_logs_no_traceback_on_every_poll(self) -> None:
        # Each resolver layer's own fail-open WARNING carries exc_info, and the
        # publish chain resolves every minute.
        degraded = mock.patch(
            "teatree.core.mode_resolution._resolve_active_mode",
            side_effect=OperationalError(_DEGRADED_STORE),
        )

        with degraded, self.assertNoLogs("teatree.core.mode_resolution"):
            braked = _dispatch_masked()

        assert braked is False

    def test_the_silence_is_scoped_to_the_resolvers_not_the_whole_process(self) -> None:
        # CONTROL for the silencing above: `resolve_standing_directives` runs in the
        # worker's thread pool, so a process-global `logging.disable` would swallow a
        # concurrent thread's unrelated records.
        bystander = logging.getLogger("teatree.tests.bystander")
        outage = OperationalError(_DEGRADED_STORE)

        def _degrade(*_args: object, **_kwargs: object) -> None:
            bystander.warning("another thread's own log, emitted mid-read")
            raise outage

        with (
            mock.patch("teatree.core.mode_resolution._resolve_active_mode", side_effect=_degrade),
            self.assertLogs("teatree.tests.bystander", level="WARNING") as captured,
        ):
            braked = _dispatch_masked()

        assert braked is False
        assert captured.output

    def test_a_raising_mode_resolver_fails_open_to_delivering(self) -> None:
        # Polarity: never suppress a rule because the brake could not be read.
        with mock.patch(_MODE_RESOLVER, side_effect=RuntimeError("no mode table")):
            braked = _dispatch_masked()
            resolved = resolve_standing_directives()

        assert braked is False
        assert len(resolved) == len(STANDING_DIRECTIVES)
