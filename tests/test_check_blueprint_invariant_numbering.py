"""Tests for the §17.1 invariant-numbering integrity gate (#836 §17.6 gate 1).

Recurring collision class: concurrent PRs each appended "the next"
§17.1 invariant number against a stale base, so the merge silently
duplicated or dropped one (occurred 3x in one session: #856/#859,
#859/#863). This gate parses §17.1's numbered list and fails when the
numbers are not a gapless 1..N with no repeats, evaluated on whatever
tree (incl. merge result) is being committed.
"""

import pytest

from scripts.hooks.check_blueprint_invariant_numbering import check_numbering, extract_invariant_numbers, main

_CLEAN = """\
## 17. Factory

### 17.1 Invariants

1. **Two layers.** Substrate vs improvement.

2. **The flywheel.** Defect to enforcement.

3. **Topology.** Orchestrator brain.

### 17.2 The flywheel

1. This numbered item is in another subsection and must be ignored.
2. So is this one.
"""

_DUPLICATE = """\
### 17.1 Invariants

1. **One.** a

2. **Two.** b

3. **Three.** c

4. **Four.** d

5. **Five.** e

6. **Six (PR A).** appended against stale base

6. **Six (PR B).** appended against the same stale base

### 17.2 Next
"""

_GAP = """\
### 17.1 Invariants

1. **One.** a

2. **Two.** b

4. **Four.** a merge dropped invariant 3

### 17.2 Next
"""


class TestExtractInvariantNumbers:
    def test_reads_only_the_17_1_block(self) -> None:
        assert extract_invariant_numbers(_CLEAN) == [1, 2, 3]

    def test_captures_duplicates_in_order(self) -> None:
        assert extract_invariant_numbers(_DUPLICATE) == [1, 2, 3, 4, 5, 6, 6]

    def test_captures_gap(self) -> None:
        assert extract_invariant_numbers(_GAP) == [1, 2, 4]

    def test_no_section_yields_empty(self) -> None:
        assert extract_invariant_numbers("no invariants section here\n1. **x.** y") == []


class TestCheckNumbering:
    def test_clean_contiguous_is_ok(self) -> None:
        result = check_numbering([1, 2, 3, 4, 5])
        assert result.ok is True

    def test_duplicate_six_fails(self) -> None:
        result = check_numbering([1, 2, 3, 4, 5, 6, 6])
        assert result.ok is False
        assert "Duplicate" in result.reason
        assert "[6]" in result.reason

    def test_duplicate_seven_fails(self) -> None:
        result = check_numbering([1, 2, 3, 4, 5, 6, 7, 7, 8])
        assert result.ok is False
        assert "Duplicate" in result.reason

    def test_gap_fails(self) -> None:
        result = check_numbering([1, 2, 4])
        assert result.ok is False
        assert "not contiguous" in result.reason

    def test_empty_fails(self) -> None:
        result = check_numbering([])
        assert result.ok is False
        assert "No numbered invariants" in result.reason


class TestMainOnRealBlueprint:
    """Anti-vacuous: run the gate against the repo's real BLUEPRINT.md."""

    def test_repo_blueprint_17_1_is_contiguous(self) -> None:
        from scripts.hooks.check_blueprint_invariant_numbering import _blueprint_path  # noqa: PLC0415

        text = _blueprint_path().read_text(encoding="utf-8")
        result = check_numbering(extract_invariant_numbers(text))
        assert result.ok is True, result.reason


class TestMain:
    def test_noop_when_blueprint_not_in_commit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import scripts.hooks.check_blueprint_invariant_numbering as mod  # noqa: PLC0415

        monkeypatch.setattr(mod, "_blueprint_in_commit", lambda: False)
        assert main() == 0

    def test_fails_on_duplicate_when_committed(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        import scripts.hooks.check_blueprint_invariant_numbering as mod  # noqa: PLC0415

        bp = tmp_path / "BLUEPRINT.md"
        bp.write_text(_DUPLICATE, encoding="utf-8")
        monkeypatch.setattr(mod, "_blueprint_in_commit", lambda: True)
        monkeypatch.setattr(mod, "_blueprint_path", lambda: bp)
        assert main() == 1

    def test_fails_on_gap_when_committed(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        import scripts.hooks.check_blueprint_invariant_numbering as mod  # noqa: PLC0415

        bp = tmp_path / "BLUEPRINT.md"
        bp.write_text(_GAP, encoding="utf-8")
        monkeypatch.setattr(mod, "_blueprint_in_commit", lambda: True)
        monkeypatch.setattr(mod, "_blueprint_path", lambda: bp)
        assert main() == 1

    def test_passes_on_clean_when_committed(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        import scripts.hooks.check_blueprint_invariant_numbering as mod  # noqa: PLC0415

        bp = tmp_path / "BLUEPRINT.md"
        bp.write_text(_CLEAN, encoding="utf-8")
        monkeypatch.setattr(mod, "_blueprint_in_commit", lambda: True)
        monkeypatch.setattr(mod, "_blueprint_path", lambda: bp)
        assert main() == 0
