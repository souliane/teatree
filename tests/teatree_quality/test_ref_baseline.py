"""The pinned-reference baseline: it is load-bearing, it fails loud, and it only shrinks.

The ratchet assertions in ``test_skill_symbol_refs.py`` are satisfied by a baseline
that agrees with the tree — including, vacuously, by one that was never read. These
tests corrupt the baseline and assert the halves go red, which is what proves the
YAML is the source of truth rather than decoration.
"""

import re
from pathlib import Path

import pytest
import yaml

from teatree.quality import ref_baseline
from teatree.quality.ref_baseline import RATCHETS, BaselineError

#: A pin the scanner cannot possibly report — the file is not in any indexed package.
_BOGUS_PIN = ("src/teatree/quality/ref_baseline.py", "teatree.definitely.not.a.real.symbol")


def _write(path: Path, mapping: dict[str, dict[str, list[str]]]) -> Path:
    path.write_text(yaml.safe_dump(mapping, sort_keys=True), encoding="utf-8")
    return path


def _live_as_mapping() -> dict[str, dict[str, list[str]]]:
    baseline = ref_baseline.load_baseline()
    out: dict[str, dict[str, list[str]]] = {}
    for name in RATCHETS:
        by_file: dict[str, list[str]] = {}
        for file_key, ref in sorted(baseline[name]):
            by_file.setdefault(file_key, []).append(ref)
        out[name] = by_file
    return out


class TestTheBaselineIsLoadBearing:
    def test_an_emptied_baseline_reds_the_new_unresolved_half(self, tmp_path: Path) -> None:
        # If the ratchets ignored the YAML, an empty one would still read as green.
        empty = _write(tmp_path / "empty.yaml", {name: {} for name in RATCHETS})
        new = ref_baseline.new_entries(path=empty)
        assert new["python_prose"], "an emptied baseline left the python_prose half green — the YAML is not read"
        assert new["charter"], "an emptied baseline left the charter half green — the YAML is not read"

    def test_the_shipped_baseline_agrees_with_the_live_tree(self) -> None:
        # Both directions of both ratchets, against the real tree: what CI asserts.
        assert ref_baseline.stale_entries() == {name: frozenset() for name in RATCHETS}
        assert ref_baseline.new_entries() == {name: frozenset() for name in RATCHETS}

    def test_a_pin_the_scanner_cannot_report_is_stale(self, tmp_path: Path) -> None:
        mapping = _live_as_mapping()
        mapping["python_prose"].setdefault(_BOGUS_PIN[0], []).append(_BOGUS_PIN[1])
        polluted = _write(tmp_path / "polluted.yaml", mapping)
        assert ref_baseline.stale_entries(path=polluted)["python_prose"] == frozenset({_BOGUS_PIN})


class TestItFailsLoudNeverSilentEmpty:
    def test_a_missing_baseline_raises_rather_than_reading_as_nothing_pinned(self, tmp_path: Path) -> None:
        with pytest.raises(BaselineError):
            ref_baseline.load_baseline(tmp_path / "absent.yaml")

    def test_unparseable_yaml_raises(self, tmp_path: Path) -> None:
        broken = tmp_path / "broken.yaml"
        broken.write_text("charter: [unclosed\n", encoding="utf-8")
        with pytest.raises(BaselineError):
            ref_baseline.load_baseline(broken)

    def test_a_dropped_ratchet_raises_rather_than_defaulting_to_empty(self, tmp_path: Path) -> None:
        partial = _write(tmp_path / "partial.yaml", {"charter": {}})
        with pytest.raises(BaselineError, match="python_prose"):
            ref_baseline.load_baseline(partial)

    def test_a_non_list_entry_raises(self, tmp_path: Path) -> None:
        wrong = tmp_path / "wrong.yaml"
        wrong.write_text(
            yaml.safe_dump({"charter": {"BLUEPRINT.md": "a-string"}, "python_prose": {}}), encoding="utf-8"
        )
        with pytest.raises(BaselineError, match=re.escape("BLUEPRINT.md")):
            ref_baseline.load_baseline(wrong)


class TestPruneOnlyEverShrinks:
    def test_it_removes_exactly_the_stale_entries(self, tmp_path: Path) -> None:
        mapping = _live_as_mapping()
        mapping["python_prose"].setdefault(_BOGUS_PIN[0], []).append(_BOGUS_PIN[1])
        polluted = _write(tmp_path / "polluted.yaml", mapping)

        removed = ref_baseline.prune(path=polluted, write=True)

        assert removed["python_prose"] == frozenset({_BOGUS_PIN})
        assert ref_baseline.load_baseline(polluted) == ref_baseline.load_baseline()

    def test_it_can_never_add_an_entry(self, tmp_path: Path) -> None:
        mapping = _live_as_mapping()
        mapping["python_prose"].setdefault(_BOGUS_PIN[0], []).append(_BOGUS_PIN[1])
        polluted = _write(tmp_path / "polluted.yaml", mapping)
        before = ref_baseline.load_baseline(polluted)

        ref_baseline.prune(path=polluted, write=True)
        after = ref_baseline.load_baseline(polluted)

        for name in RATCHETS:
            assert after[name] <= before[name], f"prune grew the {name} ratchet — it must only ever shrink"

    def test_it_is_idempotent(self, tmp_path: Path) -> None:
        mapping = _live_as_mapping()
        mapping["charter"].setdefault(_BOGUS_PIN[0], []).append(_BOGUS_PIN[1])
        polluted = _write(tmp_path / "polluted.yaml", mapping)

        ref_baseline.prune(path=polluted, write=True)
        first = polluted.read_text(encoding="utf-8")
        assert ref_baseline.prune(path=polluted, write=True) == {name: frozenset() for name in RATCHETS}
        assert polluted.read_text(encoding="utf-8") == first

    def test_check_mode_reports_without_writing(self, tmp_path: Path) -> None:
        mapping = _live_as_mapping()
        mapping["python_prose"].setdefault(_BOGUS_PIN[0], []).append(_BOGUS_PIN[1])
        polluted = _write(tmp_path / "polluted.yaml", mapping)
        before = polluted.read_text(encoding="utf-8")

        assert ref_baseline.prune(path=polluted, write=False)["python_prose"] == frozenset({_BOGUS_PIN})
        assert polluted.read_text(encoding="utf-8") == before

    def test_dump_round_trips_through_load(self, tmp_path: Path) -> None:
        target = tmp_path / "round-trip.yaml"
        original = ref_baseline.load_baseline()
        ref_baseline.dump_baseline(original, target)
        assert ref_baseline.load_baseline(target) == original
