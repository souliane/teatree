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
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from teatree.core.models import ConsolidatedMemory
from teatree.loops.dream import decay
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


_BIG_TOPIC = (
    "the worktree provision lease pid-anchored claim guard rejects an empty owner "
    "liveness probe session heartbeat expiry compare-and-swap concurrent acquire "
    "release reaper stale lease ttl budget seconds owner token isolation deadlock"
)


class BudgetDecayTierTestCase(SimpleTestCase):
    """The age+budget decay tier, INDEPENDENT of the empty ledger home-rail (#2723).

    The ledger home-rail (``prunable()``) is structurally empty for the hand-authored
    corpus (0 rows reference on-disk memories), so it can never archive the bloating
    files. This tier fires only when ``MEMORY.md`` exceeds the load budget and
    archives the oldest files that are (a) older than a hard ceiling (90d), (b)
    unreferenced, and (c) whose lesson is CAPTURED ELSEWHERE (a near-duplicate
    survivor exists) — a content/duplication check, NOT the empty ledger join. The
    age clock reads a logical ``lesson_updated`` frontmatter timestamp so cross-link /
    re-index rewrites do not reset it.
    """

    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def _decay(self, *, budget_tier: bool = False) -> decay.DecayResult:
        # Inject a no-ledger-home resolver so the budget tier is exercised in
        # isolation from the (DB-backed) ledger home-rail — this stays DB-free.
        return decay_memories(
            self.dir,
            now=_NOW,
            has_durable_home=lambda _m: False,
            policy=_policy(budget_tier=budget_tier),
        )

    def _write(self, name: str, body: str, *, age_days: int, lesson_updated: str | None = None) -> Path:
        path = self.dir / f"{name}.md"
        front = f"---\nname: {name}\ntype: feedback\n"
        if lesson_updated is not None:
            front += f"lesson_updated: {lesson_updated}\n"
        front += "---\n"
        path.write_text(f"{front}{body}\n", encoding="utf-8")
        ts = (_NOW - timedelta(days=age_days)).timestamp()
        os.utime(path, (ts, ts))
        return path

    def _write_over_budget_index(self, count: int) -> None:
        lines = "".join(
            f"- mem_{i:04d}.md — a summary that fills the index past the load budget {i}\n" for i in range(count)
        )
        (self.dir / "MEMORY.md").write_text(f"# index\n{lines}", encoding="utf-8")

    def test_over_budget_archives_old_unreferenced_duplicated_files(self) -> None:
        # MEMORY.md is over budget AND two >90d unreferenced near-duplicates exist:
        # the budget tier archives the duplicate (its lesson is captured by the twin).
        self._write("feedback_dup_keep", _BIG_TOPIC + " KEEP", age_days=120)
        archived_target = self._write("feedback_dup_drop", _BIG_TOPIC + " DROP", age_days=120)
        self._write_over_budget_index(2000)
        result = self._decay(budget_tier=True)
        assert result.archived_count >= 1
        # The duplicated file is archived (moved, never deleted), its twin survives.
        assert any(a.name == "feedback_dup_drop" for a in result.archived)
        assert not archived_target.exists()
        assert (self.dir / "archive" / "feedback_dup_drop.md").is_file()
        assert (self.dir / "feedback_dup_keep.md").exists()

    def test_nothing_archived_while_under_budget(self) -> None:
        # The SAME old, unreferenced, duplicated files — but the index is small, so
        # the budget tier does NOT fire (no pressure, nothing archived).
        self._write("feedback_dup_keep", _BIG_TOPIC + " KEEP", age_days=120)
        self._write("feedback_dup_drop", _BIG_TOPIC + " DROP", age_days=120)
        (self.dir / "MEMORY.md").write_text(
            "# index\n- feedback_dup_keep.md — a\n- feedback_dup_drop.md — b\n", encoding="utf-8"
        )
        result = self._decay(budget_tier=True)
        assert result.archived_count == 0

    def test_budget_tier_off_by_default_archives_nothing(self) -> None:
        # Without budget_tier=True the new tier never fires (no behaviour change to
        # the existing ledger-home decay path).
        self._write("feedback_dup_keep", _BIG_TOPIC + " KEEP", age_days=120)
        self._write("feedback_dup_drop", _BIG_TOPIC + " DROP", age_days=120)
        self._write_over_budget_index(2000)
        result = self._decay()
        assert result.archived_count == 0

    def test_recently_touched_lesson_is_retained_even_over_budget(self) -> None:
        # The age clock reads the logical lesson_updated frontmatter, not st_mtime:
        # a file whose st_mtime is old (a cross-link rewrite bumped it long ago) but
        # whose lesson_updated is recent is RETAINED — the lesson is fresh.
        self._write("feedback_dup_keep", _BIG_TOPIC + " KEEP", age_days=120)
        recent = (_NOW - timedelta(days=5)).date().isoformat()
        self._write("feedback_dup_fresh", _BIG_TOPIC + " FRESH", age_days=120, lesson_updated=recent)
        self._write_over_budget_index(2000)
        result = self._decay(budget_tier=True)
        # The fresh-lesson file is never archived despite being over budget + old mtime.
        assert "feedback_dup_fresh" not in {a.name for a in result.archived}
        assert (self.dir / "feedback_dup_fresh.md").exists()

    def test_referenced_file_is_retained_even_over_budget(self) -> None:
        # A file the index/another memory [[link]]s is retained even over budget.
        self._write("feedback_dup_keep", _BIG_TOPIC + " KEEP", age_days=120)
        self._write("feedback_dup_drop", _BIG_TOPIC + " DROP", age_days=120)
        # mem_other links feedback_dup_drop -> referenced -> retained.
        self._write("feedback_other", f"see [[feedback_dup_drop]] for detail {_BIG_TOPIC}", age_days=1)
        self._write_over_budget_index(2000)
        result = self._decay(budget_tier=True)
        assert "feedback_dup_drop" not in {a.name for a in result.archived}

    def test_unique_lesson_not_captured_elsewhere_is_retained(self) -> None:
        # An old, unreferenced file over budget whose lesson is UNIQUE (no
        # near-duplicate survivor) is RETAINED — captured-elsewhere is the safety rail.
        self._write("feedback_unique", _BIG_TOPIC + " a genuinely unique lesson", age_days=120)
        self._write(
            "feedback_other_topic",
            "slack notify thread timestamp channel speak tts message digest receipt reaction emoji broadcast outcome",
            age_days=120,
        )
        self._write_over_budget_index(2000)
        result = self._decay(budget_tier=True)
        assert result.archived_count == 0  # no near-duplicate survivor -> nothing safe to archive

    def test_malformed_lesson_updated_falls_back_to_mtime(self) -> None:
        # A garbage lesson_updated value falls back to the old st_mtime, so the
        # file is still treated as old and archivable.
        self._write("feedback_dup_keep", _BIG_TOPIC + " KEEP", age_days=120)
        self._write("feedback_dup_drop", _BIG_TOPIC + " DROP", age_days=120, lesson_updated="not-a-date")
        self._write_over_budget_index(2000)
        result = self._decay(budget_tier=True)
        assert result.archived_count >= 1

    def test_token_less_lesson_is_never_captured_elsewhere(self) -> None:
        # A file whose body is all stopwords has an empty token set -> it can never
        # be "captured elsewhere", so it is retained (never archived) over budget.
        self._write("feedback_empty", "a an the to of in on at is it by", age_days=120)
        self._write("feedback_other", _BIG_TOPIC + " a lesson", age_days=120)
        self._write_over_budget_index(2000)
        result = self._decay(budget_tier=True)
        assert "feedback_empty" not in {a.name for a in result.archived}

    def test_budget_tier_has_teeth(self) -> None:
        # Teeth: the SAME over-budget + old + duplicated file IS retained when the
        # tier is off, and archived when on — a vacuous tier would behave identically.
        self._write("feedback_dup_keep", _BIG_TOPIC + " KEEP", age_days=120)
        self._write("feedback_dup_drop", _BIG_TOPIC + " DROP", age_days=120)
        self._write_over_budget_index(2000)
        off = self._decay(budget_tier=False)
        assert off.archived_count == 0, "tier off must archive nothing"
        # Re-write the files (the off run did not move them) for the on run.
        on = self._decay(budget_tier=True)
        assert on.archived_count >= 1, "tier on must archive the duplicated file"
