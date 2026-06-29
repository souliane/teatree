"""Cold-tier memory RECALL — surface an archived rule when relevant (#2746).

Integration-leaning: each test writes a real cold tier (``MEMORY_ARCHIVE.md``, and
where relevant ``MEMORY.md``) under ``tmp_path`` and drives the pure scoring core.
The contract is proven in both directions — a relevant prompt surfaces the entry
top-ranked, an unrelated prompt surfaces nothing, an already-hot rule is deduped, the
output is capped, BINDING / user rules are boosted only once past the relevance floor,
and an irrelevant BINDING rule (below the floor) is NEVER surfaced.
"""

import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from teatree.loops.dream import recall
from teatree.loops.dream.recall import (
    RECALL_INJECT_LINE_MAX,
    RECALL_LIMIT,
    RECALL_MAX_BYTES,
    RecallHit,
    recall_cold_memory,
    render_recall_block,
)

_COLD_HEADER = "# Auto Memory — Cold Archive Index\n\n> preamble line.\n\n"


class RecallTestCase(SimpleTestCase):
    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def _cold(self, *lines: str) -> None:
        (self.dir / recall.COLD_INDEX_NAME).write_text(_COLD_HEADER + "\n".join(lines) + "\n", encoding="utf-8")

    def _hot(self, *lines: str) -> None:
        body = "\n".join(lines)
        (self.dir / recall.HOT_INDEX_NAME).write_text(f"# Auto Memory — Index\n\n{body}\n", encoding="utf-8")

    def _names(self, hits: list[RecallHit]) -> list[str]:
        return [hit.name for hit in hits]


class TestHit(RecallTestCase):
    def test_relevant_query_returns_the_entry_top_ranked(self) -> None:
        self._cold(
            "- feedback_worktree_first.md — always create a worktree before editing any project file",
            "- feedback_unrelated_topic.md — a note about slack reaction rendering colors",
        )
        hits = recall_cold_memory(self.dir, "how do I set up a worktree before editing project files?")
        assert hits, "a relevant query must surface the matching cold entry"
        assert hits[0].name == "feedback_worktree_first.md"

    def test_signature_only_overlap_of_two_tokens_is_a_hit(self) -> None:
        # Two distinct signature tokens overlap (no name overlap) -> clears the floor.
        self._cold("- mem_abc.md — the lease guard rejects an empty owner address")
        hits = recall_cold_memory(self.dir, "the lease guard owner check")
        assert self._names(hits) == ["mem_abc.md"]


class TestMissAndDegrade(RecallTestCase):
    def test_unrelated_query_returns_empty(self) -> None:
        self._cold("- feedback_worktree_first.md — always create a worktree before editing any project file")
        assert recall_cold_memory(self.dir, "completely unrelated quantum chromodynamics lecture") == []

    def test_render_recall_block_empty_for_no_hits(self) -> None:
        assert render_recall_block([]) == ""

    def test_missing_cold_index_returns_empty(self) -> None:
        # No MEMORY_ARCHIVE.md written at all.
        assert recall_cold_memory(self.dir, "anything at all here") == []

    def test_missing_dir_returns_empty(self) -> None:
        assert recall_cold_memory(self.dir / "absent", "anything at all here") == []

    def test_empty_query_returns_empty(self) -> None:
        self._cold("- feedback_worktree_first.md — always create a worktree before editing")
        assert recall_cold_memory(self.dir, "") == []


class TestDedupeAgainstHot(RecallTestCase):
    def test_name_already_in_hot_index_is_excluded(self) -> None:
        # Same matching tokens, but the entry is ALSO a hot pointer -> excluded.
        self._cold("- feedback_worktree_first.md — always create a worktree before editing project files")
        self._hot("- feedback_worktree_first.md — worktree first")
        assert recall_cold_memory(self.dir, "create a worktree before editing project files") == []

    def test_renamed_file_signature_substring_in_hot_is_excluded(self) -> None:
        # The cold entry's signature already lives verbatim in the hot index under a
        # DIFFERENT pointer name (a renamed file) -> the renamed-file guard drops it.
        self._cold("- feedback_old_name.md — always create a worktree before editing project files")
        self._hot("- feedback_new_name.md — always create a worktree before editing project files")
        assert recall_cold_memory(self.dir, "create a worktree before editing project files") == []

    def test_long_signature_clipped_in_hot_is_still_deduped(self) -> None:
        # A cold signature LONGER than the hot summary clip lives in the hot index
        # under a DIFFERENT name, where the reindex phase clipped it to ~110 chars.
        # The prefix-bounded renamed-file guard must still recognise it as already
        # loaded and drop the cold entry (a verbatim substring check would miss it).
        long_sig = (
            "always create a worktree before editing any project file and never edit "
            "on a shared branch under any circumstance whatsoever in this repository today"
        )
        assert len(long_sig) > recall._HOT_SUMMARY_MAX_CHARS
        self._cold(f"- feedback_cold_name.md — {long_sig}")
        self._hot(f"- feedback_hot_name.md — {long_sig[: recall._HOT_SUMMARY_MAX_CHARS]}")
        assert recall_cold_memory(self.dir, "create a worktree before editing any project file") == []


class TestCap(RecallTestCase):
    def test_at_most_recall_limit_hits(self) -> None:
        self._cold(
            *[f"- feedback_worktree_{i}.md — always create a worktree before editing files {i}" for i in range(20)]
        )
        hits = recall_cold_memory(self.dir, "create a worktree before editing project files")
        assert len(hits) == RECALL_LIMIT

    def test_block_within_byte_budget_and_lines_clipped(self) -> None:
        long_sig = "always create a worktree before editing " + "x" * 400
        self._cold(*[f"- feedback_worktree_{i}.md — {long_sig} variant{i}" for i in range(20)])
        hits = recall_cold_memory(self.dir, "create a worktree before editing project files")
        block = render_recall_block(hits)
        assert len(block.encode("utf-8")) <= RECALL_MAX_BYTES
        for line in block.splitlines()[1:]:  # skip the header line
            assert len(line) <= RECALL_INJECT_LINE_MAX


class TestBindingAndUserBoost(RecallTestCase):
    def test_binding_boost_lifts_lower_base_above_higher_base_plain(self) -> None:
        # Isolate the +3 BINDING boost: the plain entry matches MORE query tokens
        # (strictly higher base) than the BINDING entry, yet the boost lifts the
        # BINDING entry above it. Names carry no query token, so base == distinct
        # signature matches only.
        self._cold(
            "- feedback_plain.md — review worktree layout note",  # base 3
            "- feedback_binding.md — BINDING review worktree directive",  # base 2, +3
        )
        hits = recall_cold_memory(self.dir, "review worktree layout config")
        assert hits[0].name == "feedback_binding.md"
        assert hits[0].binding is True
        plain = next(hit for hit in hits if hit.name == "feedback_plain.md")
        # The plain entry has the strictly higher base (3 > 2); only the +3 boost
        # flips the order, so score == base + 3 for the BINDING winner.
        assert plain.score == 3
        assert hits[0].score == 5

    def test_user_prefixed_entry_is_boosted(self) -> None:
        self._cold(
            "- feedback_plain_rule.md — review the worktree project layout note",
            "- user_editor_choice.md — review the worktree project layout preference",
        )
        hits = recall_cold_memory(self.dir, "review the worktree project layout")
        assert hits[0].name == "user_editor_choice.md"

    def test_irrelevant_binding_below_floor_is_not_surfaced(self) -> None:
        # A BINDING entry sharing only ONE incidental signature token (below the
        # floor) is dropped despite the +3 boost — relevance gates the boost.
        self._cold("- feedback_binding_unrelated.md — BINDING a directive about slack reactions")
        hits = recall_cold_memory(self.dir, "the slack channel routing question")
        assert hits == []


class TestRelevanceFloor(RecallTestCase):
    def test_single_incidental_signature_token_is_no_hit(self) -> None:
        # One shared signature token (no name overlap) -> base 1 < floor -> dropped.
        self._cold("- feedback_misc.md — a note about reactions and colors and rendering")
        hits = recall_cold_memory(self.dir, "reactions in an otherwise totally different sentence")
        assert hits == []

    def test_single_name_token_only_overlap_is_no_hit(self) -> None:
        # REGRESSION (#2746 blocker): the query's ONLY overlap with the entry is a
        # single FILENAME token ("review") — the signature shares nothing. The old
        # scorer double-counted that one name token (entry-overlap PLUS name-overlap)
        # to fake-clear the floor of 2 and surface the rule. The floor must count
        # DISTINCT tokens, so a single name token is below it and the entry is dropped.
        self._cold("- feedback_review_team.md — slack reaction rendering colors note")
        hits = recall_cold_memory(self.dir, "review the kubernetes deployment manifest")
        assert hits == []

    def test_two_distinct_tokens_clear_the_floor(self) -> None:
        self._cold("- feedback_misc.md — a note about reactions and colors and rendering")
        hits = recall_cold_memory(self.dir, "reactions and colors discussion")
        assert self._names(hits) == ["feedback_misc.md"]


class TestAmbientStrip(RecallTestCase):
    def test_system_reminder_memory_dump_does_not_self_match(self) -> None:
        # A prompt carrying the cold entry's own signature INSIDE a <system-reminder>
        # (a MEMORY.md / CLAUDE.md dump) must NOT self-match and surface the entry.
        self._cold("- feedback_worktree_first.md — always create a worktree before editing project files")
        prompt = "<system-reminder>\nalways create a worktree before editing project files\n</system-reminder>\nhello"
        assert recall_cold_memory(self.dir, prompt) == []

    def test_unterminated_system_reminder_is_stripped(self) -> None:
        self._cold("- feedback_worktree_first.md — always create a worktree before editing project files")
        prompt = "<system-reminder>\nalways create a worktree before editing project files"
        assert recall_cold_memory(self.dir, prompt) == []

    def test_genuine_intent_outside_ambient_still_matches(self) -> None:
        # The real ask sits OUTSIDE the ambient block -> still matched.
        self._cold("- feedback_worktree_first.md — always create a worktree before editing project files")
        prompt = (
            "<system-reminder>unrelated harness noise</system-reminder>\n"
            "should I create a worktree before editing the project files?"
        )
        hits = recall_cold_memory(self.dir, prompt)
        assert self._names(hits) == ["feedback_worktree_first.md"]
