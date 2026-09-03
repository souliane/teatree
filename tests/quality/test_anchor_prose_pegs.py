"""The anchor-prose ledger: the real-tree gate plus its anti-vacuity battery.

The battery runs on a synthetic corpus with its OWN ledger, never the repo's, so
the control cases are decided by the machinery rather than by whatever the tree
happens to say today. The REWORDED case is the one that matters: it is the shape
a lexical ban is blind to by construction, and the reason the ledger exists.
"""

import re
from pathlib import Path

import pytest

from tests._generated_artifacts import DURATIONS_CASSETTE
from tests._git_repo import make_git_repo, run_git
from tests.quality._anchor_prose import (
    coverage_ratio,
    covered_bytes,
    diff_ledger,
    digest,
    doc_surface_files,
    load_ledger,
    merged_windows,
    window_digests,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: The anchor this repo currently keeps a ledger for.
_ANCHOR = "TaskCreated"

#: The conformance module whose handler cross-check scans the SAME windows.
_CROSS_CHECK_MODULE = "tests/conformance/test_task_created_is_a_task_list_event.py"

#: One claim, padded so its ±30 window is interior — a later append must not move it.
_ONE_CLAIM = "Alpha padding text here. ANCHOR is the event. Omega padding text tail here."

#: The same file after a SECOND claim is appended, separated past 2*radius so the new
#: window is a pure add rather than a merge that would re-digest the untouched one.
_TWO_CLAIMS = f"{_ONE_CLAIM}\n{'-' * 60}\nBeta. ANCHOR is also claimed here. Zeta."


def _reach_note(paths: list[str], radius: int) -> str:
    """How much of each named file the windows actually cover, for a failure to carry."""
    rows = "\n".join(
        f"  {path}: windows cover {coverage_ratio((_REPO_ROOT / path).read_text(encoding='utf-8'), _ANCHOR, radius)}"
        for path in paths
    )
    return f"REACH — a ±{radius}-char window around each literal {_ANCHOR}; the rest of each file is unseen:\n{rows}"


def _corpus_live(text: str, *, radius: int = 12) -> dict[str, list[tuple[str, str]]]:
    return {"doc.md": window_digests(text, "ANCHOR", radius)}


def _corpus_pegs(text: str, *, radius: int = 12) -> dict[str, list[str]]:
    return {"doc.md": [sha for sha, _window in window_digests(text, "ANCHOR", radius)]}


class TestMergedWindows:
    def test_each_isolated_occurrence_gets_its_own_window(self) -> None:
        assert len(merged_windows("A" + "." * 50 + "A", "A", radius=5)) == 2

    def test_overlapping_windows_merge_into_one(self) -> None:
        # Without merging these two spans overlap, so an edit between them would
        # reshuffle both digests and the ledger would churn on unrelated prose.
        assert merged_windows("A.A", "A", radius=5) == ["A.A"]

    def test_a_window_is_clipped_at_the_text_edges(self) -> None:
        assert merged_windows("ab", "a", radius=99) == ["ab"]

    def test_an_absent_anchor_yields_nothing(self) -> None:
        assert merged_windows("nothing here", _ANCHOR, radius=10) == []

    def test_a_window_carries_the_neighbouring_words(self) -> None:
        assert merged_windows("a retired ANCHOR payload", "ANCHOR", radius=10) == ["a retired ANCHOR payload"]

    def test_the_digest_is_stable_and_short(self) -> None:
        assert digest("x") == digest("x")
        assert len(digest("x")) == 16


class TestTheReachIsMeasured:
    """The number the gate reports is the real one, including when it is small."""

    def test_covered_bytes_is_the_merged_window_length(self) -> None:
        text = "Alpha. ANCHOR is the event. Omega."
        assert covered_bytes(text, "ANCHOR", radius=6) == sum(len(w) for w in merged_windows(text, "ANCHOR", 6))

    def test_a_file_without_the_anchor_covers_nothing(self) -> None:
        # The blind spot that produced the fifth residual, stated as a number.
        assert covered_bytes("no occurrence here", "ANCHOR", radius=400) == 0
        assert coverage_ratio("no occurrence here", "ANCHOR", radius=400).endswith("= 0.0%")

    def test_prose_outside_every_window_is_not_covered(self) -> None:
        text = "ANCHOR" + "." * 500
        assert covered_bytes(text, "ANCHOR", radius=10) < len(text)

    def test_the_ratio_names_covered_total_and_percent(self) -> None:
        assert coverage_ratio("..ANCHOR..", "ANCHOR", radius=2) == "10/10 = 100.0%"


class TestTheLedgerSeesWhatABanCannot:
    """Unmodified GREEN; reworded, added and deleted each RED."""

    def test_the_unmodified_corpus_is_green(self) -> None:
        assert diff_ledger(_corpus_live(_TWO_CLAIMS), _corpus_pegs(_TWO_CLAIMS)).ok

    def test_a_reworded_sentence_is_red(self) -> None:
        # No banned adjective is added or removed — a lexical ban is GREEN on both
        # sides of this edit, which is precisely the blindness being replaced.
        before = "Alpha. ANCHOR carries a dispatch. Omega."
        after = "Alpha. ANCHOR conveys a dispatch. Omega."
        drift = diff_ledger(_corpus_live(after), _corpus_pegs(before))
        assert not drift.ok
        assert len(drift.added) == 1
        assert len(drift.dropped) == 1
        assert "conveys" in drift.added[0][2]

    def test_an_added_window_is_red_and_prints_its_text(self) -> None:
        drift = diff_ledger(_corpus_live(_TWO_CLAIMS, radius=30), _corpus_pegs(_ONE_CLAIM, radius=30))
        assert [path for path, _sha, _w in drift.added] == ["doc.md"]
        assert not drift.dropped
        assert "also claimed here" in "\n".join(drift.added_lines())

    def test_a_deleted_window_is_red_and_names_the_file(self) -> None:
        drift = diff_ledger(_corpus_live(_ONE_CLAIM, radius=30), _corpus_pegs(_TWO_CLAIMS, radius=30))
        assert [path for path, _sha in drift.dropped] == ["doc.md"]
        assert not drift.added
        assert "remove the peg" in "\n".join(drift.dropped_lines())

    def test_an_unpegged_file_carrying_the_anchor_is_red(self) -> None:
        drift = diff_ledger(_corpus_live("Alpha. ANCHOR appears. Omega."), {})
        assert [path for path, _sha, _w in drift.added] == ["doc.md"]

    def test_changing_the_radius_invalidates_the_pegs(self) -> None:
        # The radius is pinned IN the ledger for exactly this reason: widening it
        # is a decision to re-read every window, not a silent re-baseline.
        text = "Alpha beta gamma. ANCHOR is the event. Delta epsilon zeta."
        assert not diff_ledger(_corpus_live(text, radius=40), _corpus_pegs(text, radius=12)).ok


class TestTheDocSurfaceExemptsTheGeneratedCassette:
    """Control: exactly one generated file is exempt, not the directory holding it."""

    def test_the_cassette_is_no_doc_surface_while_its_neighbour_is(self, tmp_path: Path) -> None:
        make_git_repo(tmp_path)
        for name in (DURATIONS_CASSETTE, "dev/handwritten.md"):
            path = tmp_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{_ANCHOR} is the event.\n")
        run_git(tmp_path, "add", "-A")
        surfaces = {p.relative_to(tmp_path).as_posix() for p in doc_surface_files(_ANCHOR, repo_root=tmp_path)}
        assert DURATIONS_CASSETTE not in surfaces
        assert "dev/handwritten.md" in surfaces


class TestTheRepoLedgerIsCurrent:
    def test_the_pegged_radius_is_what_the_ledger_declares(self) -> None:
        ledger = load_ledger(_ANCHOR)
        assert ledger.radius > 0
        assert ledger.pegs, "the ledger table is empty — the gate below would check nothing"

    def test_a_reserved_key_is_never_read_as_a_pegged_file(self) -> None:
        # Both would otherwise surface as a file the tree does not have, so the gate
        # would demand a peg be REMOVED for a path that was never a surface.
        assert not {"radius", "coverage"} & set(load_ledger(_ANCHOR).pegs)

    def test_every_doc_surface_window_is_pegged(self) -> None:
        ledger = load_ledger(_ANCHOR)
        live = {
            path.relative_to(_REPO_ROOT).as_posix(): window_digests(
                path.read_text(encoding="utf-8"), _ANCHOR, ledger.radius
            )
            for path in doc_surface_files(_ANCHOR)
        }
        drift = diff_ledger(live, ledger.pegs)
        assert drift.ok, (
            f"the prose describing {_ANCHOR} moved. READ each window below, confirm it states "
            "what is true of the event today, then update tests/quality/anchor_prose_pegs.toml.\n"
            + "\n".join([*drift.added_lines(), *drift.dropped_lines()])
            + "\n"
            + _reach_note(sorted({path for path, *_rest in [*drift.added, *drift.dropped]}), ledger.radius)
        )

    def test_the_pegged_reach_is_what_the_windows_actually_cover(self) -> None:
        # The number, not the adjective. "doc surfaces" read as coverage for five
        # residuals; a pinned numerator makes the real reach a thing a human must
        # re-affirm rather than a claim nobody ever measured.
        ledger = load_ledger(_ANCHOR)
        live = {
            path.relative_to(_REPO_ROOT).as_posix(): covered_bytes(
                path.read_text(encoding="utf-8"), _ANCHOR, ledger.radius
            )
            for path in doc_surface_files(_ANCHOR)
        }
        assert live == ledger.coverage, (
            f"the reach of the {_ANCHOR} ledger moved. Re-pin [{_ANCHOR}.coverage] in "
            "tests/quality/anchor_prose_pegs.toml, and read what the new number says about "
            f"how much of each file this instrument can see.\n{_reach_note(sorted(live), ledger.radius)}"
        )

    def test_the_conformance_cross_check_shares_the_pegged_radius(self) -> None:
        # Read textually rather than imported: a tests/quality -> tests/conformance
        # import would couple two lanes for one integer. Drift silently re-opens the
        # coverage hole 400 was chosen to close, so it is pinned rather than trusted.
        ledger = load_ledger(_ANCHOR)
        source = (_REPO_ROOT / _CROSS_CHECK_MODULE).read_text(encoding="utf-8")
        declared = re.search(r"^_RADIUS = (\d+)$", source, re.MULTILINE)
        assert declared is not None, f"{_CROSS_CHECK_MODULE} no longer declares a module-level _RADIUS"
        assert int(declared.group(1)) == ledger.radius

    @pytest.mark.parametrize("path", doc_surface_files(_ANCHOR), ids=lambda p: p.name)
    def test_no_pegged_surface_lives_under_tests(self, path: Path) -> None:
        # The ledger's own two files mention the anchor; digesting them would make
        # the ledger an input to itself, with no fixed point.
        assert not path.relative_to(_REPO_ROOT).as_posix().startswith("tests/")
