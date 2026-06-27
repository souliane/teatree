"""#786 WS5 — BLUEPRINT/docs alignment doc-invariant guard.

The #786 acceptance criterion: "BLUEPRINT.md + loop skill/docs updated
in the same change; no stale references to the retired roster model."
WS1-WS4 updated the loop architecture incrementally; WS5 is the final
alignment sweep that (a) adds one consolidated epic-completion statement
mapping the three #786 invariants to the delivered workstreams and
(b) documents #789 (subsumed, closed-as-completed) and board #50
(subsumed; the project board, not a repo issue) without reopening or
closing either.

This guard makes the "no stale references" criterion *mechanically
enforced* rather than prose-vigilance: it fails RED if the retired
immortal-singleton roster vocabulary reappears as a CURRENT-design
assertion in BLUEPRINT.md or the loop topology section, and asserts the
WS5 consolidated statement is present.
"""

import re
from pathlib import Path
from typing import ClassVar

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BLUEPRINT = REPO_ROOT / "BLUEPRINT.md"


@pytest.fixture(scope="module")
def blueprint_text() -> str:
    return BLUEPRINT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def loop_topology(blueprint_text: str) -> str:
    """The § 5.6 Loop Topology section body (up to the next ## heading)."""
    start = blueprint_text.index("### 5.6 Loop Topology")
    rest = blueprint_text[start + len("### 5.6 Loop Topology") :]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


class TestEpicCompletionStatementPresent:
    def test_consolidated_786_invariant_to_workstream_mapping_exists(self, loop_topology: str) -> None:
        # One durable statement that ties the three #786 invariants to the
        # workstreams that delivered them — not reconstructable only from
        # scattered per-WS citations.
        assert "#786" in loop_topology
        lowered = loop_topology.lower()
        assert "immortal-singleton" in lowered
        assert "fully retired" in lowered or "retired in full" in lowered
        # The three acceptance-contract invariants are named as a set.
        assert "invariant 1" in lowered
        assert "invariant 2" in lowered
        assert "invariant 3" in lowered

    def test_all_five_workstreams_plus_followup_accounted(self, loop_topology: str) -> None:
        for ws in ("WS1", "WS2", "WS3", "WS4", "WS5"):
            assert ws in loop_topology, f"{ws} missing from the epic-completion statement"


def _any_window_has(text: str, needle: str, *required: str, radius: int = 320) -> bool:
    """True if SOME ``needle`` occurrence has all ``required`` nearby.

    Within ``radius`` chars (case-insensitive). Robust to the token also
    appearing in unrelated per-WS citations elsewhere in the section.
    """
    low = text.lower()
    start = 0
    while (i := low.find(needle.lower(), start)) != -1:
        window = low[max(0, i - radius) : i + radius]
        if all(r.lower() in window for r in required):
            return True
        start = i + 1
    return False


class TestAnyWindowHasHelper:
    """Direct unit coverage of the scan-all-occurrences predicate.

    Against a well-formed BLUEPRINT an early occurrence satisfies the
    predicate and returns True, so the loop-continuation and the
    no-match exit are never exercised by the doc-invariant assertions
    alone — they need their own focused inputs.
    """

    def test_later_occurrence_matches_when_first_misses(self) -> None:
        # First "#X" has no 'ok' within radius; the second one does. The
        # match is only reachable via the `start = i + 1` continuation
        # past the non-matching first occurrence.
        text = "#X here nothing relevant. " + ("filler " * 40) + "#X and ok right next to it"
        assert _any_window_has(text, "#X", "ok", radius=20) is True

    def test_returns_false_when_no_window_satisfies(self) -> None:
        # The needle is present but no occurrence has the required token
        # nearby — exercises the loop falling through to `return False`.
        text = "#X alpha #X beta #X gamma"
        assert _any_window_has(text, "#X", "delta", radius=5) is False

    def test_returns_false_when_needle_absent(self) -> None:
        assert _any_window_has("nothing here", "#X", "ok") is False


class TestTrackABOverviewPresent:
    """The § 5.6 opening must make Track A vs Track B unmistakable (#1838).

    Anti-vacuous: revert the overview and these go RED. ``_any_window_has``
    scans all occurrences so an unrelated later mention of a token never
    satisfies the assertion on its own.
    """

    def test_track_a_described_as_no_panes_loop_restructure(self, loop_topology: str) -> None:
        assert _any_window_has(loop_topology, "Track A", "no panes", "orchestrate")
        assert _any_window_has(loop_topology, "Track A", "per-loop ownership")

    def test_track_b_described_as_pane_backed_teammates(self, loop_topology: str) -> None:
        assert _any_window_has(loop_topology, "Track B", "pane", "separate long-lived")
        assert _any_window_has(loop_topology, "Track B", "REVIEWER", "prohibited")

    def test_master_off_switch_note_present(self, loop_topology: str) -> None:
        assert _any_window_has(loop_topology, "teams", "enabled = false", "off switch")
        assert _any_window_has(loop_topology, "teams", "classic", "sub-agent")

    def test_dedicated_loops_toggle_retired_for_per_loop_default(self, loop_topology: str) -> None:
        # LOOP-PR-A deleted the dedicated_loops setting; #2650's one-`/loop`-per-
        # enabled-row model superseded the fat-`/loop`-vs-dedicated-slots toggle.
        # The vocabulary may now appear only retirement-framed (mirrors the
        # roster-retirement rule below), attributed to #2650.
        assert _any_window_has(loop_topology, "dedicated_loops", "retired", "#2650")


class TestSubsumedIssuesDocumented:
    def test_789_documented_as_subsumed_not_reopened(self, loop_topology: str) -> None:
        assert "#789" in loop_topology
        # Some #789 mention is explicitly framed as subsumed AND not reopened.
        assert _any_window_has(loop_topology, "#789", "subsume", "not")
        assert _any_window_has(loop_topology, "#789", "subsume", "closed-as-completed")

    def test_board_50_documented_as_board_not_repo_issue(self, loop_topology: str) -> None:
        assert "#50" in loop_topology
        # WS5 clarification: #50 is the project board card, NOT a repo
        # issue, and is subsumed by invariant 3 / WS4.
        assert _any_window_has(loop_topology, "#50", "board", "subsume", "not")
        assert _any_window_has(loop_topology, "#50", "repository issue")


class TestNoStaleRosterVocabularyAsCurrentDesign:
    """Retired-model vocabulary may only appear retirement-framed.

    A 'retired/no longer/replaced' framing is required; the vocabulary
    must never appear as a present-tense design assertion.
    """

    # Sentence fragments that would only appear if the doc still described
    # the immortal roster as the live mechanism.
    _STALE_AS_CURRENT: ClassVar[list[str]] = [
        r"spawn(s)? the fixed loop roster",
        r"re-spawn(s)? (the |N )?(loop )?sub-agents on (death|compaction)",
        r"coordinator (must )?keep(s)? (the )?loop sub-agents alive",
        r"resume the roster from (a |the )?brief",
    ]

    @pytest.mark.parametrize("pattern", _STALE_AS_CURRENT)
    def test_no_retired_model_as_present_tense(self, blueprint_text: str, pattern: str) -> None:
        assert re.search(pattern, blueprint_text, re.IGNORECASE) is None, (
            f"stale retired-roster phrasing present as current design: /{pattern}/"
        )

    def test_roster_mentions_are_retirement_framed(self, loop_topology: str) -> None:
        # Every "roster" mention in the loop topology must sit next to a
        # retirement word — no bare present-tense roster description.
        for m in re.finditer(r"roster", loop_topology, re.IGNORECASE):
            window = loop_topology[max(0, m.start() - 120) : m.end() + 120].lower()
            assert any(
                w in window for w in ("retire", "no roster", "nothing to re-spawn", "no fixed", "replaced", "no longer")
            ), f"bare/current-tense 'roster' mention without retirement framing near: …{window}…"
