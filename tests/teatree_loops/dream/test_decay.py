"""Phase 6 — decay/archive stale memories with a NON-VACUOUS retention guard (#1933 § 6).

Fixture-only with a FIXED clock: every test writes ``*.md`` into a tmp dir, sets
mtimes explicitly, and passes ``now=`` — never the real ``~/.claude``, never the
wall clock. The anti-vacuity contract is proven in both directions:

*   a FRESH memory is RETAINED (skipped) — freshness alone keeps it,
*   a LINKED/referenced memory is RETAINED even when old — the reference guard,
*   a memory that is old AND unreferenced AND has NO confirmed durable home in
    the ledger is RETAINED — the transfer-before-prune rail (#2546),
*   only a memory that is BOTH old AND unreferenced AND has a confirmed durable
    home is ARCHIVED (moved, never deleted, with provenance),

and the guard has TEETH: the same fresh/linked/un-homed memory IS archived once
the guard is bypassed (``test_guard_disabled_probe_archives_protected_memory``,
``test_transfer_rail_has_teeth_un_homed_archived_when_rail_off``), so a vacuous
guard that retained nothing — or archived everything — would be caught.

The file-side mechanics (freshness + reference guard) are exercised with an
``always_home`` resolver so they stay independent of the ledger; the
transfer-before-prune rail (the DB-backed default resolver) has its own
``TestCase`` block that exercises the real ``ConsolidatedMemory`` ledger.
"""

import hashlib
import os
import tempfile
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from teatree.core.models import ConsolidatedMemory
from teatree.loops.dream import decay, gates, reindex
from teatree.loops.dream.decay import BudgetTier, DecayPolicy, _MemoryFile, decay_memories, ledger_durable_home_resolver

_NOW = datetime(2026, 6, 16, 12, tzinfo=UTC)


def _policy(*, retention_days: int = 30, budget_tier: bool = False) -> DecayPolicy:
    """Build a DecayPolicy from the legacy retention/budget-tier kwargs the tests use."""
    return DecayPolicy(retention_days=retention_days, budget_tier=BudgetTier() if budget_tier else None)


def _always_home(_: _MemoryFile) -> bool:
    """A resolver that asserts every memory has a durable home — isolates the file-side guard."""
    return True


class DecayTestCase(SimpleTestCase):
    """File-side guard (freshness + reference) with a home-asserting resolver.

    These tests cover the mtime / wiki-link mechanics, NOT the ledger rail, so
    they inject ``_always_home`` to neutralise the transfer-before-prune guard
    and keep running without a database.
    """

    home_resolver: Callable[[_MemoryFile], bool] = staticmethod(_always_home)

    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def _decay(self, *, retention_days: int = 30, dry_run: bool = False) -> decay.DecayResult:
        return decay_memories(
            self.dir,
            now=_NOW,
            dry_run=dry_run,
            has_durable_home=self.home_resolver,
            policy=_policy(retention_days=retention_days),
        )

    def _write(self, name: str, body: str, *, age_days: int) -> Path:
        path = self.dir / f"{name}.md"
        path.write_text(f"name: {name}\n{body}\n", encoding="utf-8")
        ts = (_NOW - timedelta(days=age_days)).timestamp()
        os.utime(path, (ts, ts))
        return path

    def _index(self, *links: str) -> None:
        body = "".join(f"- [[{link}]]\n" for link in links)
        (self.dir / "MEMORY.md").write_text(f"# index\n{body}", encoding="utf-8")

    # ── the retention guard, both directions ────────────────────────────────

    def test_fresh_memory_is_retained(self) -> None:
        fresh = self._write("mem_fresh", "a recent lesson", age_days=1)
        result = self._decay(retention_days=30)
        assert result.archived_count == 0
        assert fresh.exists()

    def test_old_unreferenced_homed_memory_is_archived(self) -> None:
        stale = self._write("mem_stale", "an old unreferenced lesson", age_days=90)
        result = self._decay(retention_days=30)
        assert result.archived_count == 1
        assert result.archived[0].name == "mem_stale"
        assert not stale.exists()
        assert (self.dir / "archive" / "mem_stale.md").is_file()

    def test_old_but_linked_memory_is_retained(self) -> None:
        # mem_target is OLD but another live memory links it -> the REFERENCE
        # guard (not freshness) must retain it.
        target = self._write("mem_target", "old but referenced", age_days=90)
        self._write("mem_other", "see [[mem_target]] for the lease detail", age_days=1)
        result = self._decay(retention_days=30)
        assert "mem_target" not in {a.name for a in result.archived}
        assert target.exists()

    def test_old_but_index_referenced_memory_is_retained(self) -> None:
        target = self._write("mem_indexed", "old but listed in the index", age_days=90)
        self._index("mem_indexed")
        result = self._decay(retention_days=30)
        assert "mem_indexed" not in {a.name for a in result.archived}
        assert target.exists()

    def test_archive_carries_provenance_and_never_deletes(self) -> None:
        self._write("mem_stale", "the original body to preserve", age_days=120)
        self._decay(retention_days=30)
        archived = (self.dir / "archive" / "mem_stale.md").read_text(encoding="utf-8")
        assert "archived by dream decay" in archived
        assert "the original body to preserve" in archived  # content preserved, not lost

    # ── anti-vacuity: the guard has TEETH ───────────────────────────────────

    def test_guard_disabled_probe_archives_protected_memory(self) -> None:
        # The "guard-disabled probe": with the retention guard removed (archive
        # every memory regardless of age/reference), the SAME fresh + linked
        # memories ARE archived. This proves the real guard is what retains them —
        # a vacuous guard would behave identically with or without the bypass.
        fresh = self._write("mem_fresh", "a recent lesson", age_days=1)
        linked_target = self._write("mem_target", "old but referenced", age_days=90)
        self._write("mem_other", "see [[mem_target]]", age_days=1)

        # Sanity: the real guard retains both.
        guarded = self._decay(retention_days=30)
        retained_names = {fresh.stem, linked_target.stem}
        archived_names = {a.name for a in guarded.archived}
        assert not (retained_names & archived_names), "real guard must retain fresh + linked"

        # Guard-disabled probe: retention=0 days makes nothing 'fresh', and we
        # bypass the reference check by treating every loaded file as unreferenced.
        with patch.object(decay, "_is_referenced", return_value=False):
            probed = self._decay(retention_days=0)
        probed_names = {a.name for a in probed.archived}
        # With the guard bypassed, the protected memories ARE archived -> teeth.
        assert "mem_fresh" in probed_names
        assert "mem_target" in probed_names

    def test_dry_run_archives_nothing_on_disk(self) -> None:
        stale = self._write("mem_stale", "old unreferenced", age_days=90)
        result = self._decay(retention_days=30, dry_run=True)
        assert result.archived_count == 1  # decision computed
        assert stale.exists()  # but nothing moved
        assert not (self.dir / "archive").exists()

    def test_missing_dir_is_noop(self) -> None:
        result = decay_memories(self.dir / "absent", now=_NOW, has_durable_home=self.home_resolver)
        assert result.seen == 0
        assert result.archived_count == 0

    def test_unreadable_file_is_skipped_not_fatal(self) -> None:
        self._write("mem_stale", "old unreferenced", age_days=90)
        (self.dir / "broken.md").mkdir()  # makes read_text raise OSError -> skipped
        result = self._decay(retention_days=30)
        assert result.seen == 1  # only the readable memory counted


class TransferBeforePruneRailTestCase(TestCase):
    """The phase-6 transfer-before-prune rail (#2546 / #1933 § 2).

    A stale + unreferenced memory is archived ONLY when its lesson has a
    confirmed durable home in the ``ConsolidatedMemory`` ledger — a terminal
    (promoted/superseded/expired) row with a recorded ``durable_destination``
    that maps to the memory (its path is a member of ``source_files`` OR its
    name appears in ``durable_destination``). Without such a home, even an
    old + unreferenced memory is RETAINED — never pruned without transfer.

    The default resolver (``ledger_durable_home_resolver``) is the production
    seam; these tests drive ``decay_memories`` through it (no injected
    resolver), so the DB ledger is what decides.
    """

    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def _write_stale(self, name: str) -> Path:
        path = self.dir / f"{name}.md"
        path.write_text(f"name: {name}\nan old unreferenced lesson\n", encoding="utf-8")
        ts = (_NOW - timedelta(days=90)).timestamp()
        os.utime(path, (ts, ts))
        return path

    def _promoted_row(self, *, cluster: str, source_files: list[object], destination: str) -> ConsolidatedMemory:
        row = ConsolidatedMemory.record_cluster(
            cluster_key=hashlib.sha256(cluster.encode("utf-8")).hexdigest(),
            rule="A consolidated lesson with a durable home.",
            source_files=source_files,
            member_count=len(source_files) or 1,
            max_member_weight=90,
            is_binding=False,
        )
        row.mark_verified("an old unreferenced lesson")
        row.mark_promoted(destination)
        return row

    def test_stale_unreferenced_without_ledger_home_is_retained(self) -> None:
        # The rail: no ledger row homes this memory -> retained despite old+unreferenced.
        stale = self._write_stale("mem_unhomed")
        result = decay_memories(self.dir, now=_NOW)
        assert result.archived_count == 0
        assert stale.exists()

    def test_stale_unreferenced_homed_by_source_files_is_archived(self) -> None:
        stale = self._write_stale("mem_homed")
        self._promoted_row(
            cluster="homed-by-source",
            source_files=[str(stale)],
            destination="skills/rules/SKILL.md",
        )
        result = decay_memories(self.dir, now=_NOW)
        assert {a.name for a in result.archived} == {"mem_homed"}
        assert not stale.exists()

    def test_stale_unreferenced_homed_by_destination_name_is_archived(self) -> None:
        stale = self._write_stale("mem_named_home")
        self._promoted_row(
            cluster="homed-by-destination",
            source_files=["some/other/transcript.jsonl"],
            destination="mem_named_home.md",
        )
        result = decay_memories(self.dir, now=_NOW)
        assert {a.name for a in result.archived} == {"mem_named_home"}
        assert not stale.exists()

    def test_non_terminal_row_does_not_count_as_a_home(self) -> None:
        # A VERIFIED row (no terminal status, no durable_destination) is NOT a
        # confirmed home -> the memory must be retained.
        stale = self._write_stale("mem_candidate")
        row = ConsolidatedMemory.record_cluster(
            cluster_key=hashlib.sha256(b"candidate-only").hexdigest(),
            rule="A lesson still in verified state.",
            source_files=[str(stale)],
            member_count=1,
            max_member_weight=90,
            is_binding=False,
        )
        row.mark_verified("an old unreferenced lesson")  # VERIFIED, not terminal/promoted
        result = decay_memories(self.dir, now=_NOW)
        assert result.archived_count == 0
        assert stale.exists()

    def test_transfer_rail_has_teeth_un_homed_archived_when_rail_off(self) -> None:
        # Teeth: with the rail bypassed (every memory treated as homed), the SAME
        # un-homed memory the real rail retains IS archived. A vacuous rail that
        # archived regardless would behave identically with or without the bypass.
        stale = self._write_stale("mem_unhomed")

        guarded = decay_memories(self.dir, now=_NOW)
        assert guarded.archived_count == 0, "real rail must retain the un-homed memory"

        bypassed = decay_memories(self.dir, now=_NOW, has_durable_home=_always_home)
        assert {a.name for a in bypassed.archived} == {"mem_unhomed"}
        assert not stale.exists()

    def test_default_resolver_consults_prunable_ledger(self) -> None:
        # The default resolver helper is the production seam; exercise it directly.
        stale = self._write_stale("mem_probe")
        probe = _MemoryFile(path=stale, name="mem_probe", text="", mtime=_NOW)
        assert ledger_durable_home_resolver()(probe) is False
        self._promoted_row(cluster="probe", source_files=[str(stale)], destination="skills/rules/SKILL.md")
        # A fresh resolver re-reads the ledger.
        assert ledger_durable_home_resolver()(probe) is True


class BudgetDecayTierTestCase(SimpleTestCase):
    """The SCORED budget-tier RETIRE, INDEPENDENT of the empty ledger home-rail (#2723).

    The ledger home-rail (``prunable()``) is structurally empty for the hand-authored
    corpus (0 rows reference on-disk memories), so it can never archive the bloating
    files. This tier fires only when ``MEMORY.md`` is over the session-load budget and
    then archives the LOWEST-:func:`~decay._signal_score` files first — just enough to
    bring the projected hot index back under budget. A user / BINDING entry scores
    highest and is archived only if the budget forces it; a referenced file is never
    archived; every archived entry keeps its full signature in the cold
    ``MEMORY_ARCHIVE.md`` (restorable). Exercised DB-free with a no-ledger-home resolver.
    """

    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def _decay(self, *, budget_tier: bool = True, retention_days: int = 30) -> decay.DecayResult:
        # Inject a no-ledger-home resolver so the budget tier is exercised in
        # isolation from the (DB-backed) ledger home-rail — this stays DB-free.
        return decay_memories(
            self.dir,
            now=_NOW,
            has_durable_home=lambda _m: False,
            policy=_policy(retention_days=retention_days, budget_tier=budget_tier),
        )

    def _write(
        self, name: str, body: str, *, age_days: int = 120, mtype: str = "feedback", lesson_updated: str | None = None
    ) -> Path:
        # A BINDING fixture just prepends "BINDING " to *body* (no separate kwarg).
        path = self.dir / f"{name}.md"
        front = f"---\nname: {name}\n"
        if lesson_updated is not None:
            front += f"lesson_updated: {lesson_updated}\n"
        front += f"metadata:\n  type: {mtype}\n---\n"
        path.write_text(f"{front}\n{body}\n", encoding="utf-8")
        ts = (_NOW - timedelta(days=age_days)).timestamp()
        os.utime(path, (ts, ts))
        return path

    def _seed_low_signal(self, count: int, *, age_days: int = 120) -> None:
        """Seed *count* genuinely-UNIQUE, unreferenced, stale, low-signal feedback files.

        The bodies carry per-file keyword tokens so no two are near-duplicates — the
        OLD captured-elsewhere rail would retain every one (RED), the NEW signal-scored
        tier archives the lowest until under budget (GREEN).
        """
        for i in range(count):
            self._write(
                f"feedback_filler_{i:04d}",
                f"lesson keyword{i:04d}alpha keyword{i:04d}beta about a niche low-signal note",
                age_days=age_days,
            )

    def _seed_index(self) -> None:
        """Write MEMORY.md as the real rendered index of the current files (over budget when many)."""
        (self.dir / "MEMORY.md").write_text(reindex.render_index(self.dir), encoding="utf-8")

    def _rendered_line_count(self) -> int:
        return sum(1 for line in reindex.render_index(self.dir).splitlines() if line.strip())

    @staticmethod
    def _archived_sources(result: decay.DecayResult) -> set[str]:
        return {a.source.name for a in result.archived}

    def test_over_budget_archives_lowest_signal_unique_entries_until_under_budget(self) -> None:
        # M unique low-signal feedback files older than the retention window + an
        # over-budget MEMORY.md: the budget tier archives the lowest-signal ones until
        # the projected survivor index is back under budget. (RED before the fix —
        # captured-elsewhere retained every unique file.)
        self._seed_low_signal(160)
        self._seed_index()  # 162-line index -> over the 150-line budget
        result = self._decay(budget_tier=True)
        assert result.archived_count > 0
        assert self._rendered_line_count() <= gates.INDEX_LINE_BUDGET
        for archived in result.archived:
            assert (self.dir / "archive" / archived.source.name).is_file()  # moved, not deleted
            assert not (self.dir / archived.source.name).exists()

    def test_binding_and_user_entries_are_archived_last(self) -> None:
        # A mix of BINDING / user with low-signal stale, over budget: only the
        # low-signal filler is archived; the BINDING + user entries survive.
        self._seed_low_signal(160)
        keep_binding = self._write("feedback_binding_doctrine", "BINDING the load-bearing doctrine")
        keep_user = self._write("user_editor_preference", "the user's own editor preference", mtype="user")
        self._seed_index()
        result = self._decay(budget_tier=True)
        archived = self._archived_sources(result)
        assert keep_binding.name not in archived
        assert keep_user.name not in archived
        assert keep_binding.exists()
        assert keep_user.exists()
        assert any(name.startswith("feedback_filler") for name in archived)

    def test_archived_entry_is_restorable_with_provenance(self) -> None:
        self._seed_low_signal(160)
        self._seed_index()
        result = self._decay(budget_tier=True)
        assert result.archived
        for archived in result.archived:
            assert archived.destination.is_file()
            text = archived.destination.read_text(encoding="utf-8")
            assert "archived by dream decay" in text  # provenance header
            assert not archived.source.exists()  # original gone (moved, not copied)

    def test_unique_lowest_signal_lesson_is_archived_when_over_budget_with_signature_preserved(self) -> None:
        # The OLD captured-elsewhere rail RETAINED a unique lesson with no twin; the NEW
        # universal rail ARCHIVES it (over budget, lowest signal) because its full
        # signature survives in the cold MEMORY_ARCHIVE.md — a stronger durable home.
        self._seed_low_signal(159)
        unique = self._write(
            "feedback_unique_lowsig", "a genuinely unique low-signal lesson with no twin anywhere", age_days=200
        )
        self._seed_index()
        result = self._decay(budget_tier=True)
        assert unique.name in self._archived_sources(result)
        assert not unique.exists()
        cold = (self.dir / "MEMORY_ARCHIVE.md").read_text(encoding="utf-8")
        assert "feedback_unique_lowsig.md" in cold
        assert "a genuinely unique low-signal lesson with no twin anywhere" in cold

    def test_cold_index_signature_is_uncapped(self) -> None:
        # The cold MEMORY_ARCHIVE.md keeps the FULL signature (uncapped) — retention
        # needs the verbatim line — even though the hot index clips lines to 140.
        long_sig = "a long unique low-signal lesson that exceeds the hot per-line cap " + "x" * 200
        self._seed_low_signal(159)
        long_file = self._write("feedback_long_signature", long_sig, age_days=300)
        self._seed_index()
        result = self._decay(budget_tier=True)
        assert long_file.name in self._archived_sources(result)
        cold = (self.dir / "MEMORY_ARCHIVE.md").read_text(encoding="utf-8")
        cold_line = next(line for line in cold.splitlines() if line.startswith("- feedback_long_signature.md"))
        assert len(cold_line) > reindex._LINE_MAX_CHARS  # uncapped, unlike the hot index
        assert long_sig in cold_line

    def test_nothing_archived_while_under_budget(self) -> None:
        # A handful of files + a small index: the tier does not fire (no pressure).
        self._seed_low_signal(3)
        self._seed_index()
        result = self._decay(budget_tier=True)
        assert result.archived_count == 0

    def test_budget_tier_off_by_default_archives_nothing(self) -> None:
        # Without budget_tier the new tier never fires (no behaviour change to the
        # existing ledger-home decay path).
        self._seed_low_signal(160)
        self._seed_index()
        result = self._decay(budget_tier=False)
        assert result.archived_count == 0

    def test_recently_touched_lesson_is_retained_even_over_budget(self) -> None:
        # The recency signal reads the logical lesson_updated clock, not st_mtime: an
        # old-mtime file whose lesson was just updated scores high -> archived last.
        self._seed_low_signal(160)
        recent = (_NOW - timedelta(days=5)).date().isoformat()
        fresh = self._write("feedback_fresh_lesson", "a freshly updated lesson", age_days=300, lesson_updated=recent)
        self._seed_index()
        result = self._decay(budget_tier=True)
        assert fresh.name not in self._archived_sources(result)
        assert fresh.exists()

    def test_referenced_file_is_retained_even_over_budget(self) -> None:
        # A file another memory [[link]]s is never archived even when low-signal + over budget.
        self._seed_low_signal(160)
        target = self._write("feedback_referenced", "an old but referenced lesson", age_days=400)
        self._write("feedback_linker", "see [[feedback_referenced]] for the detail", age_days=1)
        self._seed_index()
        result = self._decay(budget_tier=True)
        assert target.name not in self._archived_sources(result)
        assert target.exists()

    def test_malformed_lesson_updated_falls_back_to_mtime(self) -> None:
        # A garbage lesson_updated value falls back to st_mtime -> low recency -> archivable.
        self._seed_low_signal(160)
        bad = self._write(
            "feedback_bad_date", "a lesson with a garbage clock", age_days=300, lesson_updated="not-a-date"
        )
        self._seed_index()
        result = self._decay(budget_tier=True)
        assert bad.name in self._archived_sources(result)

    def test_budget_tier_has_teeth(self) -> None:
        # Teeth: the SAME over-budget corpus archives nothing with the tier off and
        # something with it on — a vacuous tier would behave identically.
        self._seed_low_signal(160)
        self._seed_index()
        off = self._decay(budget_tier=False)
        assert off.archived_count == 0, "tier off must archive nothing"
        on = self._decay(budget_tier=True)
        assert on.archived_count > 0, "tier on must archive the lowest-signal files"

    def _seed_dense_multibyte(self, count: int, *, age_days: int = 120) -> None:
        """Seed *count* stale low-signal files whose summaries are DENSE multibyte.

        With ``_LINE_MAX_CHARS = 140`` a pure-ASCII index of ~149 lines is ≈21 KB —
        under the 24 KB byte budget — so the LINE budget always trips first and the
        BYTE branch is unreachable by ASCII alone. A summary of 3-byte UTF-8 chars
        (``—``) makes each clipped 140-char line ≈350 bytes, so a sub-150-line index
        can still blow the byte budget — the only way to exercise the byte branch.
        """
        dense = "—" * 200  # U+2014 em-dash, 3 bytes/char
        for i in range(count):
            self._write(f"feedback_mb_{i:04d}", dense, age_days=age_days)

    def test_over_byte_budget_under_line_budget_archives_until_under_byte_budget(self) -> None:
        # #2723 nit-3: the byte branch of the budget tier, reachable ONLY via
        # multibyte summaries (an ASCII index of <150 lines is always under 24 KB).
        self._seed_dense_multibyte(90)
        self._seed_index()
        before = reindex.render_index(self.dir)
        before_lines = sum(1 for line in before.splitlines() if line.strip())
        before_bytes = len(before.encode("utf-8"))
        assert before_lines <= gates.INDEX_LINE_BUDGET, "the LINE budget must NOT be the trigger"
        assert before_bytes > gates.INDEX_BYTE_BUDGET, "the BYTE budget must be exceeded"

        result = self._decay(budget_tier=True)
        assert result.archived_count > 0

        # Re-render the survivor index the way the re-index phase will write it.
        after = reindex.render_index(self.dir)
        after_lines = sum(1 for line in after.splitlines() if line.strip())
        after_bytes = len(after.encode("utf-8"))
        assert after_bytes <= gates.INDEX_BYTE_BUDGET, "the byte branch must archive until under the byte budget"
        assert after_lines < gates.INDEX_LINE_BUDGET, "lines stayed under the line budget throughout"
        after_snapshot = gates.MemorySnapshot.build(memories={}, index_text=after)
        assert gates.Gate.index_budget(after_snapshot).passed

    def test_referenced_files_are_never_archived_even_when_budget_cannot_be_met(self) -> None:
        # A hub links every filler, so the filler are all referenced (inviolable). Even
        # over budget the tier archives ONLY the unreferenced hub and exhausts its walk
        # without touching a referenced file — references win over the budget.
        self._seed_low_signal(160)
        links = " ".join(f"[[feedback_filler_{i:04d}]]" for i in range(160))
        self._write("feedback_hub", f"a hub that links everything {links}", age_days=400)
        self._seed_index()
        result = self._decay(budget_tier=True)
        archived = self._archived_sources(result)
        assert "feedback_hub.md" in archived  # the unreferenced hub is archivable
        assert not any(name.startswith("feedback_filler") for name in archived)  # every referenced file survives


class SignalScoreTestCase(SimpleTestCase):
    """Unit coverage of the pure signal-score / cold-index helpers (#2723) — DB-free."""

    @staticmethod
    def _mem(name: str, text: str, *, age_days: int = 0) -> _MemoryFile:
        return _MemoryFile(path=Path(f"{name}.md"), name=name, text=text, mtime=_NOW - timedelta(days=age_days))

    def test_user_memory_by_filename_and_by_frontmatter_type(self) -> None:
        assert decay._is_user_memory(self._mem("user_pref", "a pref"))  # filename prefix
        assert decay._is_user_memory(self._mem("misc_note", "---\nmetadata:\n  type: user\n---\nx"))  # frontmatter
        assert not decay._is_user_memory(self._mem("feedback_x", "---\nmetadata:\n  type: feedback\n---\nx"))

    def test_resolved_type_frontmatter_then_prefix_then_other(self) -> None:
        assert decay._resolved_type(self._mem("anything", "---\nmetadata:\n  type: reference\n---\nx")) == "reference"
        # an unrecognised frontmatter type falls back to the filename prefix
        assert decay._resolved_type(self._mem("project_x", "---\nmetadata:\n  type: bogus\n---\nx")) == "project"
        # no recognised type, unknown prefix -> other
        assert decay._resolved_type(self._mem("random_note", "a body")) == "other"
        # node_type is never read as type
        assert decay._resolved_type(self._mem("misc", "metadata:\n  node_type: memory\n")) == "other"

    def test_binding_detection_matches_binding_and_non_negotiable(self) -> None:
        assert decay._is_binding_text("this is a BINDING rule")
        assert decay._is_binding_text("a Non-Negotiable directive")
        assert not decay._is_binding_text("an ordinary lesson")

    def test_recency_within_window_is_max_then_decays_to_floor(self) -> None:
        retention = timedelta(days=30)
        assert decay._recency_score(self._mem("m", "x", age_days=5), _NOW, retention) == decay._SIGNAL_RECENT
        assert decay._recency_score(self._mem("m", "x", age_days=60), _NOW, retention) == decay._SIGNAL_RECENT - 30
        assert decay._recency_score(self._mem("m", "x", age_days=900), _NOW, retention) == 0  # floored

    def test_signal_score_composes_additively(self) -> None:
        user_binding = self._mem("user_rule", "BINDING the rule", age_days=1)
        score = decay._signal_score(user_binding, inbound_links=2, now=_NOW, retention=timedelta(days=30))
        assert score == 1000 + 500 + (2 * 40) + 200 + 10  # user + binding + inbound + recency + user type weight

    def test_inbound_link_counts_index_self_skip_and_cross_link(self) -> None:
        a = self._mem("mem_a", "see [[mem_b]] and [[mem_a]] (a self link is ignored)")
        b = self._mem("mem_b", "no links here")
        counts = decay._inbound_link_counts([a, b], "- index line [[mem_b]]")
        assert counts["mem_b"] == 2  # the index + mem_a
        assert counts.get("mem_a", 0) == 0  # self-link does not count as inbound

    def test_over_budget_by_bytes_or_lines(self) -> None:
        assert decay._over_budget(0, gates.INDEX_BYTE_BUDGET + 1)  # over by bytes
        assert decay._over_budget(gates.INDEX_LINE_BUDGET + 1, 0)  # over by lines
        assert not decay._over_budget(1, 1)  # under both

    def test_strip_provenance_with_without_and_malformed(self) -> None:
        prov = "<!-- archived by dream decay 2026-06-16: x; original mtime 2026-01-01 -->\nthe body\n"
        assert decay._strip_provenance(prov) == "the body\n"
        assert decay._strip_provenance("no provenance here\n") == "no provenance here\n"
        assert decay._strip_provenance("<!-- unterminated") == "<!-- unterminated"  # no closing marker -> left intact

    def test_cold_index_line_handles_unreadable_and_signatureless(self) -> None:
        d = Path(self.enterContext(tempfile.TemporaryDirectory()))
        broken = d / "broken.md"
        broken.mkdir()
        assert decay._cold_index_line(broken) == ""  # unreadable -> empty
        headings_only = d / "feedback_headings.md"
        headings_only.write_text(
            "<!-- archived by dream decay 2026-06-16: x; original mtime 2026-01-01 -->\n# Only A Heading\n",
            encoding="utf-8",
        )
        assert decay._cold_index_line(headings_only) == "- feedback_headings.md"  # no prose -> pointer only

    def test_cold_index_line_carries_frontmatter_description_not_node_type(self) -> None:
        # #2746 nit-4: an archived node-typed memory's cold-index signature is its
        # real frontmatter description, NOT the body ``node_type: memory`` line.
        d = Path(self.enterContext(tempfile.TemporaryDirectory()))
        archived = d / "feedback_kind_marker.md"
        archived.write_text(
            "<!-- archived by dream decay 2026-06-16: over-budget; original mtime 2026-01-01 -->\n"
            "---\nname: feedback_kind_marker\n"
            "description: the lease guard rejects an empty owner address\n"
            "metadata:\n  type: feedback\n---\n"
            "node_type: memory\ntrailing body\n",
            encoding="utf-8",
        )
        line = decay._cold_index_line(archived)
        assert line == "- feedback_kind_marker.md — the lease guard rejects an empty owner address"
        assert "node_type" not in line

    def test_rebuild_cold_index_noop_when_archive_absent_or_yields_no_lines(self) -> None:
        d = Path(self.enterContext(tempfile.TemporaryDirectory()))
        decay._rebuild_cold_index(d, d / "archive")  # absent
        assert not (d / "MEMORY_ARCHIVE.md").exists()
        archive = d / "archive"
        archive.mkdir()
        (archive / "broken.md").mkdir()  # unreadable -> no usable line
        decay._rebuild_cold_index(d, archive)
        assert not (d / "MEMORY_ARCHIVE.md").exists()


class OverBudgetDecayEndToEndTestCase(TestCase):
    """#2723 end-to-end: an over-budget hot index FAILS gate (d), then ONE pass fixes it.

    The budget-tier decay + re-index brings the index under budget while retention /
    no-loss / consolidation stay GREEN, and a second pass over the now-stable corpus
    archives nothing (monotonic).
    """

    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def _write(self, name: str, body: str, *, age_days: int, mtype: str = "feedback", binding: bool = False) -> Path:
        path = self.dir / f"{name}.md"
        marker = "BINDING " if binding else ""
        path.write_text(f"---\nname: {name}\nmetadata:\n  type: {mtype}\n---\n\n{marker}{body}\n", encoding="utf-8")
        ts = (_NOW - timedelta(days=age_days)).timestamp()
        os.utime(path, (ts, ts))
        return path

    def _decay(self) -> decay.DecayResult:
        return decay_memories(
            self.dir, now=_NOW, has_durable_home=lambda _m: False, policy=DecayPolicy(budget_tier=BudgetTier())
        )

    def _run_gates(
        self,
        before: gates.MemorySnapshot,
        after: gates.MemorySnapshot,
        archived: Sequence[decay.ArchivedMemory],
    ) -> gates.DreamQaReport:
        return gates.run_acceptance_pass(
            before,
            after,
            overlay="acme",
            archived=archived,
            schema_before=0,
            schema_after=0,
            maintenance_performed=True,
            persist=False,
        )

    def test_over_budget_index_fails_gate_then_decays_under_budget_next_pass(self) -> None:
        for i in range(170):
            self._write(
                f"feedback_low_{i:04d}",
                f"lesson keyword{i:04d}gamma keyword{i:04d}delta a niche low-signal note",
                age_days=120 + (i % 90),
            )
        self._write("feedback_binding_rule", "the load-bearing binding doctrine", age_days=80, binding=True)
        self._write(
            "reference_stale_note", "an old reference note nobody links to anymore", age_days=500, mtype="reference"
        )
        (self.dir / "MEMORY.md").write_text(reindex.render_index(self.dir), encoding="utf-8")

        before = gates.snapshot_memory_dir(self.dir)
        assert not gates.Gate.index_budget(before).passed  # over budget -> gate (d) FAILS

        result = self._decay()
        assert result.archived_count > 0
        reindex.reindex_memory(self.dir)  # final re-index drops the archived pointers

        after = gates.snapshot_memory_dir(self.dir)
        assert gates.Gate.index_budget(after).passed  # now under budget
        assert after.index_line_count <= gates.INDEX_LINE_BUDGET

        report = self._run_gates(before, after, result.archived)
        failed = {g.name for g in report.gate_results if not g.passed}
        assert report.passed, [g.detail for g in report.gate_results if not g.passed]
        assert {"retention", "no_loss_audit", "consolidation"}.isdisjoint(failed)

        # The BINDING entry survives (highest signal) OR its signature is in the cold index.
        cold_path = self.dir / "MEMORY_ARCHIVE.md"
        cold = cold_path.read_text(encoding="utf-8") if cold_path.exists() else ""
        assert (self.dir / "feedback_binding_rule.md").exists() or "feedback_binding_rule.md" in cold

        # Pass 2: the corpus is now under budget -> nothing archived (monotonic).
        before2 = gates.snapshot_memory_dir(self.dir)
        result2 = self._decay()
        assert result2.archived_count == 0
        reindex.reindex_memory(self.dir)
        after2 = gates.snapshot_memory_dir(self.dir)
        report2 = self._run_gates(before2, after2, result2.archived)
        mono = next(g for g in report2.gate_results if g.name == "monotonicity")
        assert mono.passed
