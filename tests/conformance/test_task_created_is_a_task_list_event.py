# test-path: cross-cutting — a whole-tree harness-contract invariant; no src/teatree/ mirror.
"""``TaskCreated`` is the task-LIST tools' event, never a dispatch seam (#4216).

Gate 17 was founded (#1488) on the premise that ``TaskCreated`` is "the one seam
the harness Workflow/Task fan-out does NOT bypass". It is not. On the installed
binary the event has exactly ONE producer — the ``TaskCreate`` tool body — so an
``Agent``/``Task``/Workflow sub-agent fan-out never reaches it, and
``teammate_name``/``team_name`` carry the CREATING session's ambient agent
identity rather than anything about a dispatch target.

That premise had spread to eleven surfaces, including the canonical harness doc
other gates are built from, so the correction needs a guard rather than a diff.
This walk pins three things: the retired vocabulary is gone from every place that
describes the event, the retired predicate NAME is gone (it asserted the same
claim in the one place a reader trusts most — the symbol), and the doc that
settles the question still carries the fact.

The ban is on ``fanned-out`` rather than on ``fan-out``: the CORRECT prose has to
say the fan-out never reaches the event, while the adjective only ever appears
describing a ``TaskCreated`` payload as a dispatch.
"""

import subprocess
from pathlib import Path

import pytest

from hooks.scripts.subagent_skill_gate import has_teammate_identity

_REPO_ROOT = Path(__file__).resolve().parents[2]
_INTERNALS_DOC = _REPO_ROOT / "docs" / "claude-code-internals.md"

#: The event whose scope the whole invariant is about.
_ANCHOR = "TaskCreated"

#: Chars either side of an anchor that count as "describing the event". Wide enough
#: to span a wrapped docstring sentence, narrow enough that a neighbouring paragraph
#: about an unrelated gate cannot bleed in.
_RADIUS = 300

#: Retired vocabulary. Naming a ``TaskCreated`` payload "fanned-out" IS the false
#: claim — the event cannot carry one.
_RETIRED_VOCABULARY = ("fanned-out", "fanned out")

#: The retired predicate name. It read as a dispatch test while reading the creator's
#: identity, which is the same false claim stated where a reader checks least.
_RETIRED_PREDICATE = "is_subagent_dispatch"

#: Substrings whose conjunction is the fact the correction rests on. Split so a
#: rewording of the sentence does not fail the guard, while deleting the claim does.
_PRODUCER_FACT = ("ONE producer", "TaskCreate` tool body")


def _tracked_files() -> list[Path]:
    """Tracked files, minus this one — the guard must be free to name what it forbids."""
    out = subprocess.run(
        ["git", "ls-files"],  # noqa: S607 — repo-relative git, no user input
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    here = Path(__file__).resolve()
    tracked = (_REPO_ROOT / line for line in out.stdout.splitlines() if line)
    return [path for path in tracked if path.is_file() and path.resolve() != here]


def _windows(text: str, anchor: str, radius: int) -> list[str]:
    """Every ``±radius`` window around an ``anchor`` occurrence, lowercased."""
    low = text.lower()
    found: list[str] = []
    start = 0
    while (i := low.find(anchor.lower(), start)) != -1:
        found.append(low[max(0, i - radius) : i + len(anchor) + radius])
        start = i + 1
    return found


def _files_describing_the_event() -> list[Path]:
    """Every tracked file that mentions the event at all."""
    return sorted(p for p in _tracked_files() if _ANCHOR in p.read_text(encoding="utf-8", errors="ignore"))


class TestWindowsHelper:
    """Anti-vacuity for the scanner the walks below rest on."""

    def test_every_occurrence_gets_its_own_window(self) -> None:
        assert len(_windows("x A y A z", "a", radius=1)) == 2

    def test_a_window_is_clipped_at_the_text_edges(self) -> None:
        assert _windows("ab", "a", radius=99) == ["ab"]

    def test_an_absent_anchor_yields_nothing(self) -> None:
        assert _windows("nothing here", _ANCHOR, radius=10) == []

    def test_a_window_carries_the_neighbouring_words(self) -> None:
        assert _windows("a fanned-out TaskCreated payload", _ANCHOR, radius=20) == ["a fanned-out taskcreated payload"]


class TestNoSurfaceCallsTheEventADispatch:
    def test_the_walk_still_finds_the_surfaces_that_describe_it(self) -> None:
        # Without this the parametrized guard silently degrades to zero cases the
        # moment the anchor spelling drifts, and reads green having checked nothing.
        names = {p.name for p in _files_describing_the_event()}
        assert names >= {"claude-code-internals.md", "CLAUDE.md", "hook_router.py", "subagent_skill_gate.py"}

    @pytest.mark.parametrize("path", _files_describing_the_event(), ids=lambda p: p.name)
    def test_no_event_mention_uses_the_retired_vocabulary(self, path: Path) -> None:
        offenders = [
            window
            for window in _windows(path.read_text(encoding="utf-8"), _ANCHOR, _RADIUS)
            if any(term in window for term in _RETIRED_VOCABULARY)
        ]
        assert offenders == [], f"{path.name} calls a {_ANCHOR} payload fanned-out: …{offenders[0]}…"


class TestTheRetiredPredicateNameIsGone:
    def test_the_scope_predicate_reads_the_creator_identity(self) -> None:
        assert has_teammate_identity({"teammate_name": "reviewer-1"}) is True
        assert has_teammate_identity({"team_name": "t3"}) is True
        assert has_teammate_identity({"task_subject": "do work"}) is False
        assert has_teammate_identity({"teammate_name": "", "team_name": "  "}) is False

    def test_no_tracked_file_still_names_the_retired_predicate(self) -> None:
        # Catches a stale import, a stale patch-target string, and a stale doc
        # reference — the patch-target one fails vacuously rather than loudly.
        stale = [
            path.relative_to(_REPO_ROOT).as_posix()
            for path in _tracked_files()
            if _RETIRED_PREDICATE in path.read_text(encoding="utf-8", errors="ignore")
        ]
        assert stale == [], f"stale '{_RETIRED_PREDICATE}' reference(s): {stale}"


class TestTheCanonicalDocStillCarriesTheFact:
    def test_the_internals_doc_states_the_single_producer(self) -> None:
        text = _INTERNALS_DOC.read_text(encoding="utf-8")
        missing = [fragment for fragment in _PRODUCER_FACT if fragment not in text]
        assert missing == [], f"{_INTERNALS_DOC.name} no longer states the producer fact: {missing}"
