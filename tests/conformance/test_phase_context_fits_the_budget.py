"""Every dispatchable phase's real system context fits the append budget untruncated.

Truncation is the degrade path, never the normal one: a truncated context loses
whole rule sections the agent never learns it lost. Measured with the real
bundle resolver and prompt builder over THIS tree's skills (the default skills dir
resolves to the main clone), with ``HOME`` pointed at an empty dir so a host's
installed harness skills cannot move the number.
"""

import os
import re
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from teatree.agents import skill_injection
from teatree.agents.context_budget import MAX_APPEND_BYTES, enforce_budget
from teatree.agents.prompt import build_system_context
from teatree.agents.skill_bundle import resolve_skill_bundle
from teatree.agents.skill_injection import _read_skill_contents_scoped
from teatree.core.modelkit.phases import KNOWN_PHASES
from teatree.core.models import Session, Task, Ticket
from teatree.skill_support.loading import SkillLoadingPolicy
from teatree.types import SkillMetadata

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SKILLS_DIR = _REPO_ROOT / "skills"

#: Room left for what a real dispatch adds on top (PR context, parent summary, survey).
_HEADROOM_BYTES = 4096

_HEADING_RE = re.compile(r"^#{2,3} .+$", re.MULTILINE)


def _rendered_context(phase: str) -> str:
    ticket = Ticket.objects.create(issue_url=f"https://example.com/issues/{abs(hash(phase)) % 100_000}")
    task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase=phase)
    skills = resolve_skill_bundle(phase=phase, overlay_skill_metadata=SkillMetadata(), worktree_path=_REPO_ROOT)
    return build_system_context(task, skills=skills, lifecycle_skill=SkillLoadingPolicy.lifecycle_for_phase(phase))


class TestEveryPhaseFitsTheBudget(TestCase):
    def test_no_phase_context_is_truncated_or_over_the_headroom(self) -> None:
        ceiling = MAX_APPEND_BYTES - _HEADROOM_BYTES
        over: list[str] = []
        with (
            tempfile.TemporaryDirectory() as home,
            patch.dict(os.environ, {"HOME": home}),
            patch.object(skill_injection, "DEFAULT_SKILLS_DIR", _SKILLS_DIR),
        ):
            for phase in sorted(KNOWN_PHASES):
                context = _rendered_context(phase)
                size = len(context.encode())
                if "…truncated" in context or size > ceiling:
                    over.append(f"{phase}: {size} B (ceiling {ceiling} B, truncated={'…truncated' in context})")
        assert not over, "phase contexts over the append budget:\n" + "\n".join(over)


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
