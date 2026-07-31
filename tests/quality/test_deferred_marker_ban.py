# test-path: cross-cutting
"""Outright ban on author-marked deferral markers in tests/, evals/ and e2e/.

A test carrying a deferred marker is a test that is not finished, and the marker
is the only thing recording that. So unlike the shrink-only ratchet over the code
roots, this gate has no ledger to bank a count in: the marker goes, or the work
does.

The rule, and the whole of it: a marker-form family from
``src/teatree/quality/incompleteness_markers.yaml`` that OPENS a comment. Three
things follow, and they are what let the guard's own tests survive it.

A comment is a note to whoever reads the file next. A docstring is the object
saying what it is, and a string literal is a runtime value -- a fixture, a
scenario's graded prose, a user-facing "renders TODO list page". Neither is
addressed to a reader, so neither is scanned; JSON and JSONL have no comment
syntax at all.

Opening the comment is what separates the marker from the mention. "TODO:" at
the head of a comment instructs; the same word inside a sentence is the file
talking ABOUT markers, which every gate that detects them has to do.

Marker form is the registry's own ``form: marker`` cut. The prose families
("for now", "not wired") describe a shape a sentence takes, which is fair enough
in a test's own commentary and is why they stay on the pegged ratchet.

This file is its own proof: it names every string the gate detects, and it is
scanned like everything else under ``tests/``.
"""

from pathlib import Path

import pytest

from teatree.quality.incompleteness_markers import (
    VERIFICATION_ROOTS,
    DeferredMarkerBan,
    MarkerForm,
    load_marker_patterns,
    scan_tree,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The four occurrences that stood in this tree when the ban was written, verbatim.
# Each survives for a different reason, and none of them for being on a list.
LEGITIMATE_OCCURRENCES: tuple[tuple[str, str, str], ...] = (
    (
        "tests/gate_contract.py",
        '"""Handlers are allowlisted with a ``# TODO(never-lockout)`` marker so they are tracked."""\n',
        "a docstring documents the convention rather than deferring work",
    ),
    (
        "tests/detector_fixture.py",
        'PROBE = \'"""TODO: something."""\'\n',
        "a fixture for the detector is a string literal, not a note",
    ),
    (
        "tests/ruff_probe.py",
        'probe.write_text("x = 1  # TODO: wire this up\\n")\n',
        "the probe proving ruff catches a marker is a string literal too",
    ),
    (
        "evals/scenarios/no_tech_debt.yaml",
        "# suppression, a TODO/FIXME-for-later, a pyproject per-file-ignore, or an\n",
        "a comment naming the anti-pattern mentions the marker mid-sentence",
    ),
)

# One planted marker per comment syntax the verification trees actually use.
BANNED_PROBES: tuple[tuple[str, str], ...] = (
    ("tests/test_probe.py", "# TODO: finish asserting the refusal\nx = 1\n"),
    ("tests/test_probe.py", "x = 1  # FIXME: this assertion is inverted\n"),
    ("evals/scenarios/probe.yaml", "# TODO: grade the negative case too\nname: probe\n"),
    ("evals/presets/probe.yaml", "# HACK: pinned until the scorer is rewritten\nname: probe\n"),
    ("e2e/dash/test_probe.py", "# XXX: this spec asserts nothing\n"),
    ("tests/quality/README.md", "- TODO: document the remaining gates\n"),
)

# Forms that carry the vocabulary without being a note to the reader.
ACCEPTED_PROBES: tuple[tuple[str, str, str], ...] = (
    ("tests/test_probe.py", 'MESSAGE = "TODO: replace this line"\n', "a string literal is a runtime value"),
    ("tests/test_probe.py", '"""TODO: something."""\n', "a docstring is the object documenting itself"),
    ("tests/test_probe.py", "# the coding TODO (higher pk) must win on rank\n", "a mention sits inside a sentence"),
    ("tests/test_probe.py", "# rendered as TODO-7 in the statusline\n", "the harness id namespace is not a marker"),
    ("evals/scenarios/probe.yaml", 'prompt: "your TODO list has TODO-50"\n', "a scalar is data, not a comment"),
    ("evals/fixtures/probe.jsonl", '{"text": "# TODO: wire this"}\n', "a fixture has no comment syntax at all"),
    ("tests/test_probe.py", "# left as a follow-up for now\n", "a prose family is not a marker form"),
)


def _plant(root: Path, rel: str, body: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


class TestTheGateBites:
    @pytest.mark.parametrize(("rel", "body"), BANNED_PROBES)
    def test_a_marker_opening_a_comment_is_rejected(self, tmp_path: Path, rel: str, body: str) -> None:
        _plant(tmp_path, rel, body)
        found = DeferredMarkerBan.over(tmp_path).markers()
        assert [marker.path for marker in found] == [rel]
        assert {marker.pattern_id for marker in found} == {"author-marker"}

    @pytest.mark.parametrize(("rel", "body"), BANNED_PROBES)
    def test_the_same_probe_was_accepted_before_the_ban(self, tmp_path: Path, rel: str, body: str) -> None:
        # The gate removed: the pre-existing tree scan reaches src/, hooks/ and
        # scripts/ only, so every probe above landed against a green build. This
        # is what the ban changes, held as a permanent assertion rather than a
        # one-off observation.
        _plant(tmp_path, rel, body)
        assert scan_tree(tmp_path) == []

    def test_a_marker_outside_the_verification_trees_is_not_this_gate_s(self, tmp_path: Path) -> None:
        _plant(tmp_path, "src/teatree/mod.py", "# TODO: finish this\nx = 1\n")
        assert DeferredMarkerBan.over(tmp_path).markers() == []

    def test_the_failure_names_the_file_line_and_remedy(self, tmp_path: Path) -> None:
        _plant(tmp_path, "tests/test_probe.py", "\n# FIXME: invert this\n")
        described = DeferredMarkerBan.over(tmp_path).markers()[0].describe()
        assert "tests/test_probe.py:2" in described
        assert "Do the work, or delete the marker" in described


class TestTheRuleNotAnAllowlist:
    @pytest.mark.parametrize(("rel", "body", "reason"), ACCEPTED_PROBES)
    def test_a_non_note_form_carrying_the_vocabulary_passes(
        self, tmp_path: Path, rel: str, body: str, reason: str
    ) -> None:
        _plant(tmp_path, rel, body)
        assert DeferredMarkerBan.over(tmp_path).markers() == [], reason

    @pytest.mark.parametrize(("rel", "body", "reason"), LEGITIMATE_OCCURRENCES)
    def test_each_standing_occurrence_passes_on_its_own_merits(
        self, tmp_path: Path, rel: str, body: str, reason: str
    ) -> None:
        # Planted alone under a bare tree: nothing about the real repo's layout
        # can be what saves it, because the real repo is not there.
        _plant(tmp_path, rel, body)
        assert DeferredMarkerBan.over(tmp_path).markers() == [], reason

    def test_prose_families_are_out_of_scope_by_registry_form(self) -> None:
        forms = {pattern.id: pattern.form for pattern in load_marker_patterns()}
        assert forms["author-marker"] is MarkerForm.MARKER
        assert set(forms.values()) - {MarkerForm.MARKER} == {MarkerForm.PROSE}

    def test_ruff_carries_the_same_vocabulary_for_the_python_half(self) -> None:
        # Ruff's FIX/TD rules are the commit-time enforcer over `.py` (pinned in
        # `test_ruff_antislop_caps.py`), and flake8-fixme's tags are fixed in
        # ruff. Holding them equal to the registry keeps ONE vocabulary: widen
        # the registry and this turns red, naming the words the fast lane will
        # not see and this gate alone has to catch.
        ban = DeferredMarkerBan.over(_REPO_ROOT)
        assert {trigger for pattern in ban.patterns for trigger in pattern.triggers} == {"todo", "fixme", "xxx", "hack"}


class TestTheLiveTree:
    def test_the_verification_trees_carry_no_banned_marker(self) -> None:
        found = DeferredMarkerBan.over(_REPO_ROOT).markers()
        assert not found, (
            "a deferred marker opens a comment under "
            + ", ".join(f"{root}/" for root in VERIFICATION_ROOTS)
            + ". A verification artefact that is not finished must not ship with a note saying so: finish "
            "it, or delete the artefact and the marker together. There is no ledger to bank this in.\n"
            + "\n".join(marker.describe() for marker in found)
        )

    def test_every_verification_root_is_actually_scanned(self) -> None:
        # A root that quietly stopped existing, or a suffix set that stopped
        # matching it, would leave the tree green for the wrong reason.
        scanned = {path.relative_to(_REPO_ROOT).parts[0] for path in DeferredMarkerBan.over(_REPO_ROOT).scanned_files}
        assert scanned == set(VERIFICATION_ROOTS)

    def test_the_trigger_prefilter_hides_no_marker(self) -> None:
        # The ban decides what to parse from a substring pass over raw text.
        # Running the full patterns over every file that pass THREW AWAY is what
        # proves a trigger is not narrower than its family.
        ban = DeferredMarkerBan.over(_REPO_ROOT)
        triggers = [trigger for pattern in ban.patterns for trigger in pattern.triggers]
        rejected = [
            path
            for path in ban.scanned_files
            if not any(trigger in path.read_text(encoding="utf-8", errors="replace").lower() for trigger in triggers)
        ]
        leaked = [marker for path in rejected for marker in ban.scan_file(path)]
        assert not leaked, "a trigger is narrower than its pattern, so the scan skipped a real marker:\n" + "\n".join(
            marker.describe() for marker in leaked
        )
