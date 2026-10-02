"""The reference-ratchet staleness scanner (#4451) — it fires on a loose ratchet and nothing else.

Driven against a real tree under ``tmp_path`` rather than a mocked scanner: the
whole point of the scanner is that it reads a clone's own baseline against that
clone's own source, so a test that stubs the read would prove nothing.
"""

from pathlib import Path

import yaml

from teatree.loop.scanners.ratchet_staleness import RATCHET_STALENESS_KIND, RatchetStalenessScanner

_MODULE = Path("src") / "teatree" / "probe_module.py"
_BASELINE = Path("src") / "teatree" / "quality" / "known_unresolved_refs.yaml"

#: A citation the tree cannot resolve, so the scanner reports it and a pin for it is LIVE.
_UNRESOLVED = "teatree.probe_module.ABSENT_SYMBOL"


def _clone(root: Path, *, cites: bool, pinned: bool) -> Path:
    """A minimal teatree-shaped clone: one module that may cite an absent symbol, one baseline."""
    module = root / _MODULE
    module.parent.mkdir(parents=True, exist_ok=True)
    body = f'"""Probe.\n\nSee :data:`{_UNRESOLVED}`.\n"""\n' if cites else '"""Probe."""\n'
    module.write_text(body, encoding="utf-8")
    (root / "hooks").mkdir(parents=True, exist_ok=True)

    pins = {str(_MODULE): [_UNRESOLVED]} if pinned else {}
    baseline = root / _BASELINE
    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.write_text(yaml.safe_dump({"charter": {}, "python_prose": pins}), encoding="utf-8")
    return root


class TestItFiresOnlyOnAStalePin:
    def test_a_pin_whose_citation_was_deleted_is_reported(self, tmp_path: Path) -> None:
        # The #4451 shape exactly: the pin survived, the citation it named did not.
        scanner = RatchetStalenessScanner(repo=_clone(tmp_path, cites=False, pinned=True))

        (signal,) = scanner.scan()

        assert signal.kind == RATCHET_STALENESS_KIND
        assert signal.payload["stale"] == [["python_prose", str(_MODULE), _UNRESOLVED]]
        assert "1 stale" in signal.summary

    def test_a_pin_whose_citation_still_stands_is_silent(self, tmp_path: Path) -> None:
        assert RatchetStalenessScanner(repo=_clone(tmp_path, cites=True, pinned=True)).scan() == []

    def test_a_clean_tree_is_silent(self, tmp_path: Path) -> None:
        assert RatchetStalenessScanner(repo=_clone(tmp_path, cites=False, pinned=False)).scan() == []


class TestItNeverAbortsTheTick:
    def test_a_clone_with_no_baseline_is_silent(self, tmp_path: Path) -> None:
        # A clone older than #4451 has no ratchet to be stale against — not a failure.
        (tmp_path / "src" / "teatree").mkdir(parents=True)
        assert RatchetStalenessScanner(repo=tmp_path).scan() == []

    def test_an_unparseable_baseline_is_absorbed_rather_than_raised(self, tmp_path: Path) -> None:
        # The read fails loud into the log; the tick must still reach the scanners behind this one.
        root = _clone(tmp_path, cites=False, pinned=True)
        (root / _BASELINE).write_text("python_prose: [unclosed\n", encoding="utf-8")
        assert RatchetStalenessScanner(repo=root).scan() == []


class TestItIsReadOnly:
    def test_scanning_leaves_the_clone_byte_identical(self, tmp_path: Path) -> None:
        # A dirty tree would make pull_main_clone skip its fast-forward — one stale
        # artifact traded for another.
        root = _clone(tmp_path, cites=False, pinned=True)
        before = {p: p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}

        RatchetStalenessScanner(repo=root).scan()

        after = {p: p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}
        assert after == before
