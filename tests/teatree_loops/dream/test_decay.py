"""Phase 6 — decay/archive stale memories with a NON-VACUOUS retention guard (#1933 § 6).

Fixture-only with a FIXED clock: every test writes ``*.md`` into a tmp dir, sets
mtimes explicitly, and passes ``now=`` — never the real ``~/.claude``, never the
wall clock. The anti-vacuity contract is proven in both directions:

*   a FRESH memory is RETAINED (skipped) — freshness alone keeps it,
*   a LINKED/referenced memory is RETAINED even when old — the reference guard,
*   only a memory that is BOTH old AND unreferenced is ARCHIVED (moved, never
    deleted, with provenance),

and the guard has TEETH: the same fresh/linked memory IS archived once the guard
is bypassed (``test_guard_disabled_probe_archives_protected_memory``), so a
vacuous guard that retained nothing — or archived everything — would be caught.
"""

import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from teatree.loops.dream import decay
from teatree.loops.dream.decay import decay_memories

_NOW = datetime(2026, 6, 16, 12, tzinfo=UTC)


class DecayTestCase(SimpleTestCase):
    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))

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
        result = decay_memories(self.dir, now=_NOW, retention_days=30)
        assert result.archived_count == 0
        assert fresh.exists()

    def test_old_unreferenced_memory_is_archived(self) -> None:
        stale = self._write("mem_stale", "an old unreferenced lesson", age_days=90)
        result = decay_memories(self.dir, now=_NOW, retention_days=30)
        assert result.archived_count == 1
        assert result.archived[0].name == "mem_stale"
        assert not stale.exists()
        assert (self.dir / "archive" / "mem_stale.md").is_file()

    def test_old_but_linked_memory_is_retained(self) -> None:
        # mem_target is OLD but another live memory links it -> the REFERENCE
        # guard (not freshness) must retain it.
        target = self._write("mem_target", "old but referenced", age_days=90)
        self._write("mem_other", "see [[mem_target]] for the lease detail", age_days=1)
        result = decay_memories(self.dir, now=_NOW, retention_days=30)
        assert "mem_target" not in {a.name for a in result.archived}
        assert target.exists()

    def test_old_but_index_referenced_memory_is_retained(self) -> None:
        target = self._write("mem_indexed", "old but listed in the index", age_days=90)
        self._index("mem_indexed")
        result = decay_memories(self.dir, now=_NOW, retention_days=30)
        assert "mem_indexed" not in {a.name for a in result.archived}
        assert target.exists()

    def test_archive_carries_provenance_and_never_deletes(self) -> None:
        self._write("mem_stale", "the original body to preserve", age_days=120)
        decay_memories(self.dir, now=_NOW, retention_days=30)
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
        guarded = decay_memories(self.dir, now=_NOW, retention_days=30)
        retained_names = {fresh.stem, linked_target.stem}
        archived_names = {a.name for a in guarded.archived}
        assert not (retained_names & archived_names), "real guard must retain fresh + linked"

        # Guard-disabled probe: retention=0 days makes nothing 'fresh', and we
        # bypass the reference check by treating every loaded file as unreferenced.
        with patch.object(decay, "_is_referenced", return_value=False):
            probed = decay_memories(self.dir, now=_NOW, retention_days=0)
        probed_names = {a.name for a in probed.archived}
        # With the guard bypassed, the protected memories ARE archived -> teeth.
        assert "mem_fresh" in probed_names
        assert "mem_target" in probed_names

    def test_dry_run_archives_nothing_on_disk(self) -> None:
        stale = self._write("mem_stale", "old unreferenced", age_days=90)
        result = decay_memories(self.dir, now=_NOW, retention_days=30, dry_run=True)
        assert result.archived_count == 1  # decision computed
        assert stale.exists()  # but nothing moved
        assert not (self.dir / "archive").exists()

    def test_missing_dir_is_noop(self) -> None:
        result = decay_memories(self.dir / "absent", now=_NOW)
        assert result.seen == 0
        assert result.archived_count == 0

    def test_unreadable_file_is_skipped_not_fatal(self) -> None:
        self._write("mem_stale", "old unreferenced", age_days=90)
        (self.dir / "broken.md").mkdir()  # makes read_text raise OSError -> skipped
        result = decay_memories(self.dir, now=_NOW, retention_days=30)
        assert result.seen == 1  # only the readable memory counted
