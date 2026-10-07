"""Every dispatchable phase's real system context fits the append budget untruncated, or degrades legibly.

Truncation is the degrade path, never the normal one: a truncated context loses
whole rule sections the agent never learns it lost. Measured with the real
bundle resolver and prompt builder over THIS tree's skills (the default skills dir
resolves to the main clone), with ``HOME`` pointed at an empty dir so a host's
installed harness skills cannot move the number, and the skills dir counted at one
canonical path so the checkout's location cannot move it either.
"""

import os
import re
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from teatree.agents import prompt
from teatree.agents.context_budget import MAX_APPEND_BYTES, enforce_budget
from teatree.agents.prompt import build_system_context
from teatree.agents.skill_assurance import _explicit_directive_names
from teatree.agents.skill_bundle import resolve_skill_bundle, stage_skills_for_dispatch
from teatree.agents.skill_injection import _read_skill_contents_scoped
from teatree.contrib.t3_teatree.overlay import TeatreeOverlay
from teatree.core.modelkit.phases import KNOWN_PHASES
from teatree.core.models import Session, Task, Ticket
from teatree.skill_support import index as skill_index
from teatree.skill_support.loading import SkillLoadingPolicy
from teatree.types import SkillMetadata

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SKILLS_DIR = _REPO_ROOT / "skills"

#: Room left for what a real dispatch adds on top (PR context, parent summary, survey).
_HEADROOM_BYTES = 4096

#: Stands in for the checkout's skills dir, so a long worktree path and CI's `/app` get one verdict.
_CANONICAL_SKILLS_DIR = "/opt/teatree/skills"

_HEADING_RE = re.compile(r"^#{2,3} .+$", re.MULTILINE)
_COMPANION_RE = re.compile(r"^- (?P<name>\S+): not embedded", re.MULTILINE)
_MARKER_RE = re.compile(r"\[…truncated (?P<dropped>\d+) bytes[^\]]*\]")

#: Inside the 1,174-3,496 B the overlay-active reviewing render measured over the budget.
_FORCED_OVERAGE_BYTES = 3000

#: An overage a few KB wide must cost a few KB, not a whole 60 KB section.
_MAX_ELIDED_BYTES = 8192


def _dispatch_task(phase: str) -> Task:
    ticket = Ticket.objects.create(issue_url=f"https://example.com/issues/{abs(hash(phase)) % 100_000}")
    return Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase=phase)


#: The pinned ``ac-reviewing-codebase`` the review run embeds is ~35 KiB; the stand-in leaves room for it to grow.
_REVIEW_COMPANION_STAND_IN_BYTES = 48 * 1024


def _rendered_context(task: Task) -> str:
    stage = stage_skills_for_dispatch(task.phase)
    skills = resolve_skill_bundle(
        phase=task.phase, overlay_skill_metadata=SkillMetadata(), detection_root=_REPO_ROOT, stage_skills=stage
    )
    return build_system_context(
        task, skills=skills, lifecycle_skill=SkillLoadingPolicy.lifecycle_for_phase(task.phase), stage_skills=stage
    )


def _measured_bytes(context: str, skills_dir: Path) -> int:
    return len(context.replace(str(skills_dir), _CANONICAL_SKILLS_DIR).encode())


class TestEveryPhaseFitsTheBudget(TestCase):
    def test_no_phase_context_is_truncated_or_over_the_headroom(self) -> None:
        ceiling = MAX_APPEND_BYTES - _HEADROOM_BYTES
        over: list[str] = []
        with (
            tempfile.TemporaryDirectory() as home,
            patch.dict(os.environ, {"HOME": home}),
            patch.object(skill_index, "DEFAULT_SKILLS_DIR", _SKILLS_DIR),
        ):
            for phase in sorted(KNOWN_PHASES):
                context = _rendered_context(_dispatch_task(phase))
                size = _measured_bytes(context, _SKILLS_DIR)
                if "…truncated" in context or size > ceiling:
                    over.append(f"{phase}: {size} B (ceiling {ceiling} B, truncated={'…truncated' in context})")
        assert not over, "phase contexts over the append budget:\n" + "\n".join(over)

    def test_the_review_run_fits_with_its_full_generic_companion_embedded(self) -> None:
        ceiling = MAX_APPEND_BYTES - _HEADROOM_BYTES
        body = "---\nname: ac-reviewing-codebase\n---\n" + "x" * _REVIEW_COMPANION_STAND_IN_BYTES + "\n"
        with (
            tempfile.TemporaryDirectory() as home,
            patch.dict(os.environ, {"HOME": home}),
            patch.object(skill_index, "DEFAULT_SKILLS_DIR", _SKILLS_DIR),
        ):
            companion = Path(home) / ".agents" / "skills" / "ac-reviewing-codebase" / "SKILL.md"
            companion.parent.mkdir(parents=True)
            companion.write_text(body, encoding="utf-8")
            context = _rendered_context(_dispatch_task("architectural_review"))
            size = _measured_bytes(context, _SKILLS_DIR)
        assert f"--- SKILL: ac-reviewing-codebase ---\n{body}" in context
        assert "…truncated" not in context
        assert size <= ceiling, f"review run: {size} B (ceiling {ceiling} B)"

    def test_the_measure_does_not_depend_on_the_checkout_path(self) -> None:
        task = _dispatch_task("reviewing")
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"HOME": home}):
            short_dir = Path(home) / "s"
            long_dir = Path(home) / ("a-long-worktree-checkout-directory-name" * 3) / "skills"
            long_dir.parent.mkdir()
            for link in (short_dir, long_dir):
                link.symlink_to(_SKILLS_DIR, target_is_directory=True)
            measured: dict[Path, tuple[int, int]] = {}
            for skills_dir in (short_dir, long_dir):
                with patch.object(skill_index, "DEFAULT_SKILLS_DIR", skills_dir):
                    context = _rendered_context(task)
                measured[skills_dir] = (len(context.encode()), _measured_bytes(context, skills_dir))
        assert measured[short_dir][0] != measured[long_dir][0], "the checkout path no longer reaches the context"
        assert measured[short_dir][1] == measured[long_dir][1], measured


class TestRulesCoreSurvivesAnOverrun(TestCase):
    def test_an_over_budget_bundle_sheds_the_lifecycle_tail_not_the_rules_core(self) -> None:
        block = _read_skill_contents_scoped(
            ["rules", "review"], primary_skills={"rules", "review"}, skills_dir=_SKILLS_DIR
        )
        max_bytes = MAX_APPEND_BYTES // 2
        assert len(block.encode()) > max_bytes, "the bundle no longer overruns — the cut is not exercised"

        out = enforce_budget(block, [(block, "the skill body")], max_bytes=max_bytes)

        core = (_SKILLS_DIR / "rules" / "SKILL.md").read_text(encoding="utf-8")
        lost = [heading for heading in _HEADING_RE.findall(core) if heading not in out]
        assert not lost, f"rules core headings cut by the budget pass: {lost[:5]}"
        assert "open one with the Read tool" in out, "the skill-file reach line was cut"


class TestStackDirectivesSurviveAnOverrun(TestCase):
    def test_an_over_budget_bundle_keeps_both_stack_directives(self) -> None:
        block = _read_skill_contents_scoped(
            ["rules", "review", "ac-django", "ac-python"], primary_skills={"rules", "review"}, skills_dir=_SKILLS_DIR
        )
        max_bytes = MAX_APPEND_BYTES // 2
        assert len(block.encode()) > max_bytes, "the bundle no longer overruns — the cut is not exercised"

        out = enforce_budget(block, [(block, "the skill body")], max_bytes=max_bytes)

        assert {"ac-django", "ac-python"} <= _explicit_directive_names(out)


class TestOverlayActiveReviewingDegradesLegibly(TestCase):
    """The reviewing render with the teatree overlay active stays usable when it runs over.

    Rendered with the overlay's real skill metadata, so the companion skills a
    live reviewer gets are in the bundle; the budget is forced just under the
    untruncated size, the margin a live render actually ran over.
    """

    def _render(self, task: Task, *, max_bytes: int) -> str:
        overlay = TeatreeOverlay()
        with (
            patch("teatree.core.overlay_loader.get_overlay", return_value=overlay),
            patch.object(prompt, "MAX_APPEND_BYTES", max_bytes),
        ):
            skills = resolve_skill_bundle(
                phase=task.phase,
                overlay_skill_metadata=overlay.metadata.get_skill_metadata(),
                detection_root=_REPO_ROOT,
            )
            return build_system_context(
                task, skills=skills, lifecycle_skill=SkillLoadingPolicy.lifecycle_for_phase(task.phase)
            )

    def test_an_overrun_keeps_the_companion_list_the_skill_tool_and_the_review_workflows(self) -> None:
        task = _dispatch_task("reviewing")
        with (
            tempfile.TemporaryDirectory() as home,
            patch.dict(os.environ, {"HOME": home}),
            patch.object(skill_index, "DEFAULT_SKILLS_DIR", _SKILLS_DIR),
        ):
            full = self._render(task, max_bytes=10**7)
            cut = self._render(task, max_bytes=len(full.encode()) - _FORCED_OVERAGE_BYTES)

        companions = _COMPANION_RE.findall(full)
        marker = _MARKER_RE.search(cut)
        assert companions, "the overlay-active reviewing bundle no longer lists a companion skill"
        assert marker, "the forced overage did not truncate the context"
        assert not [name for name in companions if f"- {name}: not embedded" not in cut], marker[0]
        assert "Skill tool" in marker[0], marker[0]
        assert "no Skill tool" not in marker[0], marker[0]
        assert "\n## Workflows\n" in cut, marker[0]
        assert int(marker["dropped"]) < _MAX_ELIDED_BYTES, marker[0]
