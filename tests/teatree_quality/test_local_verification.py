"""Every per-phase local-verification mandate is diff-scoped (souliane/teatree#3994).

The RED this pins: with the scoped-lane prescription removed, a one-file change still
triggers a full local sweep. Restore ``uv run pytest --no-cov -x -q`` to
``skills/code/SKILL.md`` and ``test_registered_mandates_are_diff_scoped`` fails.

The surface paths are spelled out literally below on purpose: a prose-only diff has no
import edges, so ``teatree.quality.doc_impact``'s reference-reader map is the only route
that selects this test — and it selects a test by the paths its SOURCE names.
"""

from pathlib import Path

import pytest

from teatree.quality import full_suite_invocation
from teatree.quality.local_verification import (
    CITED_NOT_PRESCRIBED,
    PHASE_MANDATES,
    SCOPED_LANE_TOKENS,
    Finding,
    PhaseMandate,
    scan_mandates,
    scan_text,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

_EXPECTED_SURFACES = (
    "CLAUDE.md",
    "AGENTS.md",
    "README.md",
    "docs/contributing.md",
    "tests/README.md",
    "skills/code/SKILL.md",
    "skills/test/SKILL.md",
    "skills/ship/SKILL.md",
    "skills/contribute/SKILL.md",
    "skills/retro/references/commit-to-fork.md",
    "src/teatree/agents/coding_prompt.py",
    "tests/CLAUDE.md",
)


class TestRegistry:
    def test_registry_covers_every_phase_mandate_surface(self) -> None:
        assert tuple(m.surface for m in PHASE_MANDATES) == _EXPECTED_SURFACES

    def test_every_registered_surface_exists_on_disk(self) -> None:
        missing = [m.surface for m in PHASE_MANDATES if not (_REPO_ROOT / m.surface).is_file()]
        assert not missing, f"registered mandate surface(s) no longer on disk: {missing}"


class TestRegisteredMandatesAreDiffScoped:
    def test_registered_mandates_are_diff_scoped(self) -> None:
        findings = scan_mandates(_REPO_ROOT)
        assert not findings, (
            "a phase's local-verification mandate is not diff-scoped -- the full suite ran "
            "locally twice per ticket before CI ran it a third time (#3994):\n" + "\n".join(str(f) for f in findings)
        )


class TestScanText:
    def _mandate(self) -> PhaseMandate:
        return PhaseMandate(surface="skills/code/SKILL.md", phase="coding")

    def _roots(self) -> tuple[str, ...]:
        # Module-qualified: pytest's default ``python_functions = test*`` would otherwise
        # collect a bare ``declared_testpaths`` import as a test and error on its parameter.
        return full_suite_invocation.declared_testpaths(_REPO_ROOT / "pyproject.toml")

    def test_scoped_lane_with_no_full_suite_is_clean(self) -> None:
        text = "Run `bash dev/test-affected.sh` before pushing."
        assert scan_text(text, self._mandate(), self._roots()) == []

    def test_unscoped_full_suite_is_reported(self) -> None:
        text = "Run `bash dev/test-affected.sh`, then `uv run pytest --no-cov -x -q`."
        details = [f.detail for f in scan_text(text, self._mandate(), self._roots())]
        assert any("UNSCOPED whole-suite pytest" in d for d in details), details

    def test_missing_scoped_lane_is_reported(self) -> None:
        # Absence-satisfied guard: deleting the mandate must NOT read as compliant.
        details = [f.detail for f in scan_text("No test guidance here.", self._mandate(), self._roots())]
        assert any("names no diff-scoped lane" in d for d in details), details

    def test_backticked_invocation_is_not_hidden_from_the_matcher(self) -> None:
        # shlex keeps a trailing backtick on the token, so ``pytest` `` would never match.
        text = '5. **All tests pass:** `cd "$T3_REPO" && uv run pytest` -- and `bash dev/test-affected.sh`.'
        details = [f.detail for f in scan_text(text, self._mandate(), self._roots())]
        assert any("UNSCOPED whole-suite pytest" in d for d in details), details

    def test_fenced_block_invocation_is_reported(self) -> None:
        text = "Run:\n\n```bash\nbash dev/test-affected.sh\nuv run pytest --no-cov -x -q\n```\n"
        details = [f.detail for f in scan_text(text, self._mandate(), self._roots())]
        assert any("UNSCOPED whole-suite pytest" in d for d in details), details

    def test_scoped_node_run_is_allowed(self) -> None:
        text = "Run `uv run pytest tests/teatree_quality/test_x.py -q` then `bash dev/test-affected.sh`."
        assert scan_text(text, self._mandate(), self._roots()) == []

    def test_prose_after_a_scoped_command_is_not_a_full_suite(self) -> None:
        # The matcher treats trailing tokens as positionals, so a sentence continuing past
        # an inline command used to read as `pytest tests` and false-flag a correct line.
        text = (
            "Run `uv run pytest tests/<mirror>/test_<leaf>.py -q` and `bash dev/test-affected.sh`. "
            "New code ships with its tests in the same commit."
        )
        assert scan_text(text, self._mandate(), self._roots()) == []

    @pytest.mark.parametrize("token", SCOPED_LANE_TOKENS)
    def test_each_scoped_lane_token_satisfies_the_positive_half(self, token: str) -> None:
        assert scan_text(f"Run `{token}`.", self._mandate(), self._roots()) == []


class TestCitedNotPrescribedPragma:
    """A prohibition reads identically to a prescription, so the author declares which."""

    def _mandate(self) -> PhaseMandate:
        return PhaseMandate(surface="skills/test/SKILL.md", phase="testing")

    def _roots(self) -> tuple[str, ...]:
        return full_suite_invocation.declared_testpaths(_REPO_ROOT / "pyproject.toml")

    def test_pragma_line_is_not_read_as_a_prescription(self) -> None:
        text = (
            f"Never a bare `uv run pytest` against the containers. <!-- {CITED_NOT_PRESCRIBED} -->\n"
            "Run `bash dev/test-affected.sh` instead.\n"
        )
        assert scan_text(text, self._mandate(), self._roots()) == []

    def test_pragma_inside_a_fenced_block_is_not_read_as_a_prescription(self) -> None:
        text = f"```bash\nbash dev/test-affected.sh\nuv run pytest  # {CITED_NOT_PRESCRIBED} -- what NOT to do\n```\n"
        assert scan_text(text, self._mandate(), self._roots()) == []

    def test_pragma_is_line_scoped_not_file_scoped(self) -> None:
        # The escape must not silence the rest of the surface: an unmarked full-suite
        # prescription two lines later is still a finding.
        text = (
            f"Never a bare `uv run pytest`. <!-- {CITED_NOT_PRESCRIBED} -->\n"
            "Run `bash dev/test-affected.sh`.\n"
            "Then `uv run pytest --no-cov -x -q`.\n"
        )
        details = [f.detail for f in scan_text(text, self._mandate(), self._roots())]
        assert any("UNSCOPED whole-suite pytest" in d for d in details), details

    def test_pragma_cannot_satisfy_the_positive_half(self) -> None:
        # Marking every line still leaves the surface naming no scoped lane.
        text = f"Never a bare `uv run pytest`. <!-- {CITED_NOT_PRESCRIBED} -->\n"
        details = [f.detail for f in scan_text(text, self._mandate(), self._roots())]
        assert [d for d in details if "names no diff-scoped lane" in d], details


class TestScanMandates:
    def test_unreadable_surface_is_a_finding(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n', encoding="utf-8")
        mandate = PhaseMandate(surface="skills/gone/SKILL.md", phase="coding")
        findings = scan_mandates(tmp_path, (mandate,))
        assert [f.surface for f in findings] == ["skills/gone/SKILL.md"]
        assert "unreadable" in findings[0].detail

    def test_finding_renders_surface_phase_and_detail(self) -> None:
        rendered = str(Finding(surface="skills/code/SKILL.md", phase="coding", detail="boom"))
        assert rendered == "skills/code/SKILL.md [coding]: boom"
