# test-path: cross-cutting
"""Shrink-only ratchet on prose in which code declares its own incompleteness.

The failure this exists for: a change titled as making a config file the source
of truth shipped a file that was generated and never read, a whole phase was
skipped, and the feature was declared done. The tell was in the shipped module's
own docstring the entire time -- it said the tier did not reach the resolver and
that a carve-out was kept with nothing in it. CI was green, because nothing
looked for that sentence.

Shape, not one string: the phrase families live in
``src/teatree/quality/incompleteness_markers.yaml`` and the walk in
``teatree.quality.incompleteness_markers``. This module supplies the ledger
discipline, in the idiom of ``test_intra_core_deferred_import_ratchet.py``:
per-file pegs in ``incompleteness_marker_pegs.toml``, over-peg blocks, under-peg
passes. Per-file keying makes the ledger set-union mergeable, and same-file
contention surfaces as a git textual conflict rather than a post-merge red.

Resolving an unfinished statement never blocks. The freed budget does sit in
exactly the file most likely to grow the next one, so lowering the entry to bank
it keeps the ledger an honest census rather than an allowance — but that is a
one-line edit, not a red build. Compelling it made every finished phase pay for
finishing, which is how a ledger of unfinished work stops shrinking.

``TestClosedIssueSubGate`` is the deterministic half. A deferral naming a closed
issue is either done-but-uncleaned or a promise nobody tracks; either way the
prose is wrong. It reads the committed ``closed_issues.toml`` snapshot and never
the network -- see that file for the polarity argument.
"""

import tomllib
from pathlib import Path

import pytest

from teatree.quality.incompleteness_markers import (
    ONE_SHOT_DESIGN_DOCS,
    Marker,
    applicable_patterns,
    issue_deferrals,
    load_marker_patterns,
    per_file_counts,
    scan_file,
    scan_tree,
    scanned_files,
)
from tests.quality._deferred_imports import diff_pegs, load_pegs

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PEGS = Path(__file__).resolve().parent / "incompleteness_marker_pegs.toml"
_CLOSED_ISSUES = Path(__file__).resolve().parent / "closed_issues.toml"

# The module docstring as it shipped, reconstructed from the change that made
# this gate necessary. Two statements, both true of the code at the time, both
# invisible to every check the repo then had.
HISTORICAL_DOCSTRING = '''"""Effective-settings resolution -- the partition + env + the autonomy collapse.

Every settings field has exactly one home. The file config tier was removed, so
every field is DB-home now -- the TOML carve-out is retained but empty (a file
tier would be a deliberate, tested re-introduction).

A scalar field resolves to its dataclass default. The TOML-default tier is NOT
wired into the resolver (a later phase -- ``config/schema.py``), so a defaults
file adopting a live value that differs from the conservative code default MUST
write a row on import.
"""

def get_effective_settings() -> dict[str, str]:
    return {}
'''

# The same module with the phase finished: it describes what the code does, and
# claims nothing about work still owed. The control that proves the detector
# discriminates rather than matching any settings-resolution prose.
FINISHED_DOCSTRING = '''"""Effective-settings resolution -- the partition + env + the autonomy collapse.

Every settings field has exactly one home, and every home is the DB. A scalar
field resolves from the schema default, which the resolver reads directly, so a
defaults file and the resolver cannot disagree about a field's value.
"""

def get_effective_settings() -> dict[str, str]:
    return {}
'''


def _write_module(root: Path, body: str) -> Path:
    path = root / "src" / "teatree" / "config" / "resolution.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def tree_markers() -> list[Marker]:
    return scan_tree(_REPO_ROOT)


class TestHistoricalCase:
    """The gate must be demonstrably RED on the change that motivated it."""

    def test_fires_on_the_shipped_docstring(self, tmp_path: Path) -> None:
        _write_module(tmp_path, HISTORICAL_DOCSTRING)
        found = scan_tree(tmp_path)
        assert {marker.pattern_id for marker in found} == {"retained-but-empty", "not-wired", "later-phase"}

    def test_blocks_that_file_against_an_empty_ledger(self, tmp_path: Path) -> None:
        _write_module(tmp_path, HISTORICAL_DOCSTRING)
        drift = diff_pegs(per_file_counts(scan_tree(tmp_path)), {})
        assert [path for path, _live, _peg in drift.over_peg] == ["src/teatree/config/resolution.py"]

    def test_silent_once_the_phase_is_finished(self, tmp_path: Path) -> None:
        _write_module(tmp_path, FINISHED_DOCSTRING)
        assert scan_tree(tmp_path) == []

    def test_the_historical_statements_stay_resolved_in_this_tree(self, tree_markers: list[Marker]) -> None:
        # The change that motivated this gate wired the TOML tier in and deleted the
        # prose, so the live tree carries none of those statements and the peg is gone.
        # Pinned here rather than dropped: the fixture cases above prove the detector
        # discriminates, and this proves the real file it was built for stays clean.
        live = [marker for marker in tree_markers if marker.path == "src/teatree/config/resolution.py"]
        assert live == [], [marker.describe() for marker in live]


class TestMarkerRatchet:
    def test_no_file_exceeds_its_peg(self, tree_markers: list[Marker]) -> None:
        counts = per_file_counts(tree_markers)
        drift = diff_pegs(counts, load_pegs("markers", toml_path=_PEGS))
        over = {path for path, _live, _peg in drift.over_peg}
        detail = [marker.describe() for marker in tree_markers if marker.path in over]
        assert not drift.over_peg, (
            "code gained prose declaring its own incompleteness. Shipping a comment or docstring that "
            "admits the implementation is unfinished is inadmissible: finish it, or do not ship the phase. "
            "Where the statement genuinely must stand, raise the file's entry in "
            "tests/quality/incompleteness_marker_pegs.toml [markers] with the reason in the commit "
            "message -- banking is an attributable line in the diff, never automatic.\n" + "\n".join(detail)
        )


class TestClosedIssueSubGate:
    @staticmethod
    def _closed() -> set[int]:
        return set(tomllib.loads(_CLOSED_ISSUES.read_text(encoding="utf-8"))["closed"])

    @staticmethod
    def _banked() -> dict[str, set[int]]:
        table = tomllib.loads(_PEGS.read_text(encoding="utf-8"))["closed_issue_deferrals"]
        return {path: set(issues) for path, issues in table.items()}

    def test_no_unbanked_deferral_points_at_a_closed_issue(self, tree_markers: list[Marker]) -> None:
        closed, banked = self._closed(), self._banked()
        offenders = [
            deferral
            for deferral in issue_deferrals(tree_markers)
            if deferral.issue in closed and deferral.issue not in banked.get(deferral.marker.path, set())
        ]
        assert not offenders, (
            "a deferral points at a CLOSED issue. Either the work landed and the prose is stale, or the "
            "promise is orphaned and nothing is tracking it. Resolve the prose, or record the site in "
            "tests/quality/incompleteness_marker_pegs.toml [closed_issue_deferrals]:\n"
            + "\n".join(deferral.describe() for deferral in offenders)
        )

    def test_banked_entries_still_describe_live_prose(self, tree_markers: list[Marker]) -> None:
        live = {(deferral.marker.path, deferral.issue) for deferral in issue_deferrals(tree_markers)}
        stale = sorted(
            f"  - {path} no longer defers to #{issue}"
            for path, issues in self._banked().items()
            for issue in issues
            if (path, issue) not in live
        )
        assert not stale, (
            "the closed-issue bank outlived the prose it describes. Drop the entry from "
            "tests/quality/incompleteness_marker_pegs.toml [closed_issue_deferrals]:\n" + "\n".join(stale)
        )

    def test_manifest_records_every_issue_a_marker_names(self, tree_markers: list[Marker]) -> None:
        # Fail-open means an unrecorded issue can never block, which is the right
        # polarity but also a silent hole. Naming the gap here keeps the refresh
        # a visible chore rather than an invisible loss of coverage.
        manifest = tomllib.loads(_CLOSED_ISSUES.read_text(encoding="utf-8"))
        known = set(manifest["closed"]) | set(manifest["open"])
        missing = sorted({deferral.issue for deferral in issue_deferrals(tree_markers)} - known)
        assert not missing, (
            "a deferral names an issue with no recorded state, so the sub-gate cannot judge it. Run "
            "`uv run python scripts/refresh_closed_issue_manifest.py` and commit the result. Missing: "
            + ", ".join(f"#{issue}" for issue in missing)
        )


class TestTriggerPreFilter:
    def test_a_rejected_file_could_not_have_matched(self) -> None:
        # The trigger literals halve the scan by deciding what to parse, which is
        # only safe if they can never hide a marker. Reading the regexes does not
        # prove that; running the full pattern set over every file the filter
        # THREW AWAY does, and an over-narrow trigger surfaces here as a leak.
        patterns = load_marker_patterns()
        rejected = [
            path
            for path in scanned_files(_REPO_ROOT)
            if not applicable_patterns(path.read_text(encoding="utf-8", errors="replace"), patterns)
        ]
        leaked = [marker for path in rejected for marker in scan_file(path, patterns, repo_root=_REPO_ROOT)]
        assert not leaked, "a trigger is narrower than its pattern, so the scan skipped a real marker:\n" + "\n".join(
            marker.describe() for marker in leaked
        )


class TestScope:
    def test_one_shot_design_docs_stay_out_of_scope(self) -> None:
        # Owner decision: a dated one-shot design doc is a cleanup candidate under
        # CLAUDE.md's no-historical-narration rule, not a ratchet input -- its
        # volume would swing the count for reasons unrelated to code. Pinned so
        # widening the doc scope cannot pull one back in unnoticed.
        scanned = {path.relative_to(_REPO_ROOT).as_posix() for path in scanned_files(_REPO_ROOT)}
        for excluded in ONE_SHOT_DESIGN_DOCS:
            assert (_REPO_ROOT / excluded).is_file(), f"{excluded} is gone — drop it from ONE_SHOT_DESIGN_DOCS"
            assert excluded not in scanned

    def test_registry_phrases_do_not_make_the_detector_its_own_offender(self) -> None:
        module = _REPO_ROOT / "src" / "teatree" / "quality" / "incompleteness_markers.py"
        assert scan_file(module, load_marker_patterns(), repo_root=_REPO_ROOT) == []

    def test_string_literals_are_not_prose(self, tmp_path: Path) -> None:
        # A PR-body template or user-facing message carrying a deferral word is a
        # runtime value, not the module describing itself.
        _write_module(tmp_path, 'WHY_PLACEHOLDER = "TODO: replace this line with the rationale"\n')
        assert scan_tree(tmp_path) == []
