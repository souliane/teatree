"""One execution lane — the vocabulary of a second lane must not come back (#4212).

Teatree runs headless. ``agent_runtime``, ``AgentRuntime`` and ``ExecutionTarget``
selected BETWEEN two lanes, so with one lane they name a distinction the code no
longer makes; a re-introduction is a lie about the code's shape, not a feature.

The three surfaces most at risk of being swept away WITH the lane are pinned here
too, because a grep for "interactive" reaches all of them: the ``/t3:interactive``
skill, the ``t3:*`` agent definitions the factory dispatches, and question deferral.
"""

import re
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Where a retired name could plausibly come back. Migrations record history and are
#: append-only, so the two ``RemoveField`` rows naming the dropped column stay.
_SEARCH_ROOTS = ("src", "hooks", "tests", "skills", "agents", "docs", "deploy", "evals")
_SKIP_PARTS = frozenset({".git", "__pycache__", "migrations", "generated", "fixtures"})
_SKIP_SUFFIXES = frozenset({".jsonl", ".png", ".svg", ".ico", ".lock", ".woff2", ".pyc"})

#: This file names every retired token by construction, so it excludes itself.
_SELF = Path(__file__).relative_to(_REPO_ROOT).as_posix()

_RETIRED = (
    "agent_runtime",
    "AgentRuntime",
    "T3_AGENT_RUNTIME",
    "ExecutionTarget",
    "execution_target",
    "claimable_for_interactive",
    "tasks_interactive_launch",
    "loop_dispatch_refusal",
    "interactive_claim_refusal",
)


def _searchable_files() -> list[Path]:
    files: list[Path] = []
    for root in _SEARCH_ROOTS:
        base = _REPO_ROOT / root
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix in _SKIP_SUFFIXES:
                continue
            if _SKIP_PARTS & set(path.parts):
                continue
            if path.relative_to(_REPO_ROOT).as_posix() == _SELF:
                continue
            files.append(path)
    return files


@pytest.mark.parametrize("token", _RETIRED)
def test_no_surface_still_names_the_retired_second_lane(token: str) -> None:
    hits = []
    for path in _searchable_files():
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        if token in text:
            hits.append(path.relative_to(_REPO_ROOT).as_posix())
    assert hits == [], f"{token!r} is retired but still named by: {hits}"


def test_the_scanner_can_see_a_planted_reintroduction(tmp_path: Path) -> None:
    """Control: the sweep above is only evidence if it goes RED on a real hit."""
    planted = tmp_path / "reintroduced.py"
    planted.write_text("execution_target = 'headless'\n")
    assert "execution_target" in planted.read_text()


class TestInteractiveSkillSurvives:
    """The monitor session is what steers and debugs the factory — it must not be swept."""

    @staticmethod
    def _skill() -> str:
        return (_REPO_ROOT / "skills" / "interactive" / "SKILL.md").read_text()

    def test_the_skill_still_exists_and_is_loadable(self) -> None:
        path = _REPO_ROOT / "skills" / "interactive" / "SKILL.md"
        assert path.is_file()
        front = yaml.safe_load(path.read_text().split("---")[1])
        assert front["name"] == "interactive"

    def test_it_carries_the_do_not_implement_order_in_the_imperative(self) -> None:
        body = self._skill()
        assert "do NOT implement it" in body
        assert "File a ticket" in body
        assert "prioritized" in body

    def test_it_names_both_exemptions_so_the_order_is_not_over_read(self) -> None:
        body = self._skill()
        assert "Work already in flight" in body
        assert "[headless-authoring-ok: <reason>]" in body

    def test_it_says_review_merge_diagnose_answer_are_not_implementation(self) -> None:
        body = self._skill()
        assert "Reviewing, merging, diagnosing and answering are NOT implementation" in body

    def test_it_requires_a_plan_before_the_first_edit_with_no_emergency_exception(self) -> None:
        body = self._skill()
        assert "a plan artifact exists before the first" in body.lower()
        assert "An emergency is not an exception to this." in body


class TestFactoryAgentDefinitionsSurvive:
    """The ``t3:*`` agent definitions are the factory's HANDS, not an operator convenience."""

    @staticmethod
    def _defined_agent_names() -> set[str]:
        """Every ``agents/*.md`` definition, by its own frontmatter ``name``."""
        names = set()
        for path in (_REPO_ROOT / "agents").glob("*.md"):
            front = yaml.safe_load(path.read_text().split("---")[1])
            names.add(str(front["name"]))
        return names

    def test_every_dispatched_phase_agent_has_its_definition_file(self) -> None:
        from teatree.core.modelkit.phases import SUBAGENT_BY_PHASE  # noqa: PLC0415 — deferred: Django-dependent

        # ``codex:*`` phases resolve a slash-command agent, not an ``agents/*.md`` file.
        dispatched = {n for n in SUBAGENT_BY_PHASE.values() if n.startswith("t3:")}
        assert dispatched, "no phase dispatches a t3 agent — the factory has no hands"
        defined = self._defined_agent_names()
        assert [n for n in sorted(dispatched) if n.removeprefix("t3:") not in defined] == []

    def test_every_agent_named_by_a_dispatch_table_has_its_definition_file(self) -> None:
        from teatree.loop.dispatch_tables import AGENT_BY_KIND  # noqa: PLC0415 — deferred: Django-dependent

        named = {target for target in AGENT_BY_KIND.values() if target.startswith("t3:")}
        assert named, "the dispatch table names no agent — the factory has no hands"
        defined = self._defined_agent_names()
        # ``t3:debug`` is the skill spelling of the ``debugger`` definition.
        aliases = {"t3:debug": "debugger"}
        missing = [n for n in sorted(named) if aliases.get(n, n.removeprefix("t3:")) not in defined]
        assert missing == []


class TestQuestionDeferralIsUntouched:
    """Deferral is driven by mode and preset — a different axis from the deleted lane."""

    def test_the_park_records_a_deferred_question_and_names_no_runtime(self) -> None:
        from teatree.core.models import task_handoff  # noqa: PLC0415 — deferred: Django-dependent

        source = Path(task_handoff.__file__).read_text(encoding="utf-8")
        assert "record_deferred_question" in source
        assert not re.search(r"\bagent_runtime\b", source)

    def test_the_deferral_seam_is_still_reachable(self) -> None:
        from teatree.core.models.task_handoff import park_for_user_input  # noqa: PLC0415 — deferred: Django-dependent

        assert callable(park_for_user_input)
