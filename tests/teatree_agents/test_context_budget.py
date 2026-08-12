"""Tests for teatree.agents.context_budget — the E2BIG append byte budget."""

import logging
import re
from pathlib import Path

import pytest

from teatree.agents.context_budget import MAX_APPEND_BYTES, enforce_budget
from teatree.agents.skill_bundle import resolve_skill_bundle
from teatree.agents.skill_injection import _read_skill_contents_scoped
from teatree.skill_support.loading import SkillLoadingPolicy
from teatree.types import SkillMetadata

#: The repo's own skills tree, so the production-bundle measurement is the same
#: on every host regardless of ``T3_REPO`` or an installed ``~/.claude/skills``.
_REPO_SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"

_HEADING_RE = re.compile(r"^## .+$", re.MULTILINE)
_MARKER_RE = re.compile(r"\n\[…truncated .*?\]", re.DOTALL)


class TestEnforceBudget:
    def test_under_budget_is_byte_identical(self) -> None:
        text = "small context " + "x" * 100
        assert enforce_budget(text, [("x" * 100, "somewhere")], max_bytes=MAX_APPEND_BYTES) is text

    def test_truncates_first_block_first(self) -> None:
        big = "B" * 5000
        second = "S" * 5000
        text = f"head\n{big}\nmid\n{second}\ntail"
        out = enforce_budget(text, [(big, "block-one"), (second, "block-two")], max_bytes=6000)

        assert len(out.encode()) <= 6000
        # The first block absorbs the whole overage; the second stays intact.
        assert "…truncated" in out
        assert "see block-one" in out
        assert second in out

    def test_spills_to_second_block_when_first_insufficient(self) -> None:
        first = "A" * 2000
        second = "C" * 20000
        text = f"{first}\n{second}"
        out = enforce_budget(text, [(first, "first"), (second, "second")], max_bytes=4000)

        assert len(out.encode()) <= 4000
        assert "see first" in out
        assert "see second" in out

    def test_marker_reports_dropped_byte_count(self) -> None:
        block = "Z" * 10000
        out = enforce_budget(block, [(block, "the artifact")], max_bytes=1000)

        assert len(out.encode()) <= 1000
        assert "bytes; see the artifact" in out

    def test_multibyte_block_never_splits_a_codepoint(self) -> None:
        block = "é" * 10000  # 2 bytes each in UTF-8
        out = enforce_budget(block, [(block, "unicode")], max_bytes=1000)

        assert len(out.encode()) <= 1000
        out.encode()  # a split codepoint would already have raised on decode

    def test_empty_block_is_skipped(self) -> None:
        text = "H" * 5000
        # A missing (empty) block contributes nothing and must not crash.
        out = enforce_budget(text, [("", "absent"), (text, "present")], max_bytes=1000)
        assert len(out.encode()) <= 1000
        assert "see present" in out


class TestPhantomBlockReclaimsNothing:
    """A block that is not a substring of *text* must not spend the budget.

    ``enforce_budget`` truncates by exact-substring replace, so a caller that
    passes a block the assembled context never embedded reclaims zero bytes. If
    the pass credits those bytes anyway, ``overage`` reaches 0 on a phantom and
    the loop exits before it reaches the real, genuinely over-budget block —
    which is how the E2BIG spawn failure of #4386 shipped a 144 KB append.
    """

    def test_absent_block_reclaims_nothing_and_the_real_block_absorbs_it(self) -> None:
        real = "R" * 20000
        phantom = "P" * 20000  # never appears in `text` — the non-planning survey shape
        text = f"head\n{real}\ntail"

        out = enforce_budget(text, [(phantom, "the phantom"), (real, "the real block")], max_bytes=5000)

        assert len(out.encode()) <= 5000
        assert "see the real block" in out
        assert "see the phantom" not in out

    def test_a_phantom_mid_list_does_not_strand_the_block_that_can_pay(self) -> None:
        # The phantom sits between a block too small to clear the overage and the
        # one large enough to: crediting it exits the pass with the payer intact.
        small = "S" * 3000
        payer = "B" * 20000
        text = f"{small}\n{payer}"

        out = enforce_budget(text, [(small, "small"), ("Q" * 20000, "phantom"), (payer, "payer")], max_bytes=5000)

        assert len(out.encode()) <= 5000
        assert "see payer" in out


class TestBudgetPostcondition:
    """The returned text is within budget or the pass says so — never both silently.

    The arithmetic is not the contract; the byte count is. A pass that reports
    success while returning an over-budget string surfaces as an opaque
    ``[Errno 7] Argument list too long`` at spawn, far from the cause.
    """

    def test_unabsorbable_overage_is_still_cut_to_the_budget(self) -> None:
        out = enforce_budget("U" * 10000, [], max_bytes=1000)

        assert len(out.encode()) <= 1000

    def test_unabsorbable_overage_is_logged_at_error(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.ERROR, logger="teatree.agents.context_budget"):
            enforce_budget("U" * 10000, [], max_bytes=1000)

        assert [r for r in caplog.records if r.levelno >= logging.ERROR]

    def test_a_budget_met_by_the_blocks_logs_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        block = "B" * 10000
        with caplog.at_level(logging.ERROR, logger="teatree.agents.context_budget"):
            enforce_budget(f"head\n{block}", [(block, "the block")], max_bytes=5000)

        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def _sectioned_block(sections: int, *, body_bytes: int, skill: str = "rules") -> str:
    """A skill-embed-shaped block: the ``--- SKILL: ---`` header then N ``## `` sections."""
    parts = [f"--- SKILL: {skill} ---", "Intro prose before the first heading."]
    parts.extend(f"## Section {i}\n{'s' * body_bytes}" for i in range(sections))
    return "\n".join(parts)


def _kept_text(truncated: str) -> str:
    """*truncated* with its trailing elision marker removed."""
    return _MARKER_RE.sub("", truncated)


def _cut_lands_on_a_heading_boundary(original: str, truncated: str) -> bool:
    """Whether *truncated* is *original* cut exactly where a ``## `` section starts.

    A byte-prefix cut lands at an arbitrary offset — mid-sentence, mid-word — so
    the surviving text ends with a partial section the reader cannot identify.
    A section-aware cut ends either at the block's end or immediately before a
    ``## `` heading, so every section that survived survived whole.
    """
    kept = _kept_text(truncated)
    if not original.startswith(kept):
        return False
    remainder = original[len(kept) :]
    return not remainder or remainder.lstrip("\n").startswith("## ")


class TestSectionAwareTruncation:
    """An over-budget block is cut on a ``## `` boundary and names what it dropped.

    A byte-prefix cut discards 40-60% of a production skill bundle at an arbitrary
    offset and tells the agent nothing about which rules went; the agent cannot
    recover them (this lane has no Skill tool). Dropping whole sections off the
    tail and naming them converts silent mid-sentence loss into legible elision.
    """

    def test_cut_lands_on_a_section_boundary(self) -> None:
        block = _sectioned_block(20, body_bytes=500)
        out = enforce_budget(block, [(block, "the skill body")], max_bytes=5000)

        assert len(out.encode()) <= 5000
        assert _cut_lands_on_a_heading_boundary(block, out)

    def test_marker_names_every_dropped_heading(self) -> None:
        block = _sectioned_block(6, body_bytes=500)
        out = enforce_budget(block, [(block, "the skill body")], max_bytes=2200)

        kept_headings = set(_HEADING_RE.findall(_kept_text(out)))
        dropped = [h for h in _HEADING_RE.findall(block) if h not in kept_headings]
        assert dropped, "the fixture must actually overflow for this assertion to bite"
        for heading in dropped:
            assert heading.removeprefix("## ") in out

    def test_marker_qualifies_a_dropped_heading_with_its_skill(self) -> None:
        block = _sectioned_block(6, body_bytes=500, skill="ship")
        out = enforce_budget(block, [(block, "the skill body")], max_bytes=2200)

        assert "ship §" in out

    def test_unsectioned_block_still_fits_via_the_byte_prefix(self) -> None:
        # A JSON survey / prose summary carries no `## ` sections to drop; the
        # byte-prefix fallback keeps the E2BIG bound absolute.
        block = "{" + "j" * 20000 + "}"
        out = enforce_budget(block, [(block, "the survey")], max_bytes=1000)

        assert len(out.encode()) <= 1000
        assert "see the survey" in out

    def test_preamble_larger_than_the_budget_still_fits(self) -> None:
        # Every section dropped and the pre-heading preamble alone still overruns
        # — the byte-prefix fallback must still bound it.
        block = "P" * 20000 + "\n## Only Section\nbody"
        out = enforce_budget(block, [(block, "the skill body")], max_bytes=1000)

        assert len(out.encode()) <= 1000


@pytest.mark.parametrize("phase", ["coding", "reviewing", "shipping", "testing", "planning"])
class TestProductionSkillBundleFitsOrElidesLegibly:
    """Every real per-phase skill bundle either fits or truncates legibly.

    Measured on this repo's own ``skills/`` tree, so the regression is the same
    on every host. Each production bundle is 1.7-2.3x the append budget today, so
    every agent dispatch truncates — the contract is that what survives is
    whole sections and the marker names the rest.
    """

    def _bundle_block(self, phase: str) -> str:
        skills = resolve_skill_bundle(phase=phase, overlay_skill_metadata=SkillMetadata())
        lifecycle = SkillLoadingPolicy.lifecycle_for_phase(phase)
        return _read_skill_contents_scoped(
            skills,
            primary_skills={lifecycle} if lifecycle else set(),
            skills_dir=_REPO_SKILLS_DIR,
        )

    def test_bundle_fits_or_cuts_on_a_heading_boundary(self, phase: str) -> None:
        block = self._bundle_block(phase)
        out = enforce_budget(block, [(block, "the skill body")], max_bytes=MAX_APPEND_BYTES)

        assert len(out.encode()) <= MAX_APPEND_BYTES
        assert _cut_lands_on_a_heading_boundary(block, out)

    def test_truncated_bundle_names_what_it_dropped(self, phase: str) -> None:
        block = self._bundle_block(phase)
        out = enforce_budget(block, [(block, "the skill body")], max_bytes=MAX_APPEND_BYTES)
        if out == block:
            pytest.skip(f"{phase} bundle fits the budget — nothing was dropped")

        kept_headings = set(_HEADING_RE.findall(_kept_text(out)))
        dropped = [h for h in _HEADING_RE.findall(block) if h not in kept_headings]
        assert dropped, "an over-budget bundle must have dropped at least one section"
        assert "dropped whole sections" in out
        assert dropped[0].removeprefix("## ") in out
