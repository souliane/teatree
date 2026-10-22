"""Production-sized read-only review inputs shared by brief conformance tests."""

import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from teatree.agents import phase_blocks
from teatree.agents.prompt import build_read_only_system_context, build_read_only_task_prompt
from teatree.agents.read_only_venue_brief import ReadOnlyFacts
from teatree.agents.skill_bundle import resolve_skill_bundle, stage_skills_for_dispatch
from teatree.contrib.t3_teatree.overlay import TeatreeOverlay
from teatree.core.models import Session, Task, Ticket
from teatree.skill_support import index as skill_index
from teatree.skill_support.loading import SkillLoadingPolicy

ROOT = Path(__file__).resolve().parents[2]
SKILLS = ROOT / "skills"
REVIEW_DIFF = "```diff\n@@ example @@\n```python\nt3 teatree ticket show 123\n```\n" + "x" * 23_900 + "\n```"
RUBRIC = (
    "",
    "TICKET RUBRIC:",
    *(f"  #{i} The reviewer checks case {i}." for i in range(15)),
    "  #15 Run t3 teatree ticket show 123 and check the result.",
)
SEAMS = ("", "PLAN — declared integration seams:", "  - Run t3 teatree ticket show 123 for the plan.")
STAND_IN_BYTES = {"ac-django": 28_300, "ac-python": 12_800, "writing-plans": 5_000}


@contextmanager
def read_only_review_world() -> Generator[None]:
    with TemporaryDirectory() as home:
        install_root = Path(home) / ".agents" / "skills"
        for name, size in STAND_IN_BYTES.items():
            skill = install_root / name / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text(f"---\nname: {name}\n---\n" + "x" * size, encoding="utf-8")
        overlay = TeatreeOverlay()
        with (
            patch.dict(os.environ, {"HOME": home}),
            patch.object(skill_index, "install_roots", lambda: [install_root]),
            patch.object(skill_index, "DEFAULT_SKILLS_DIR", SKILLS),
            patch("teatree.core.overlay_loader.get_overlay", return_value=overlay),
            patch.object(phase_blocks, "rubric_brief_lines", return_value=RUBRIC),
            patch.object(phase_blocks, "declared_seams_brief_lines", return_value=SEAMS),
            patch.object(phase_blocks, "review_diff_brief_lines", return_value=("", "DIFF UNDER REVIEW:", REVIEW_DIFF)),
        ):
            yield


def read_only_context(phase: str, *, reviewer: bool = False) -> str:
    ticket = Ticket.objects.create(
        issue_url=(
            f"https://github.com/example-org/example-repo/{'pull' if reviewer else 'issues'}/"
            f"{Ticket.objects.count() + 5}"
        ),
        role=Ticket.Role.REVIEWER if reviewer else Ticket.Role.AUTHOR,
        state=Ticket.State.WORK_STARTED,
    )
    task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase=phase)
    stage = stage_skills_for_dispatch(phase)
    skills = resolve_skill_bundle(
        phase=phase,
        overlay_skill_metadata=TeatreeOverlay().metadata.get_skill_metadata(),
        detection_root=ROOT,
        stage_skills=stage,
    )
    return build_read_only_system_context(
        task, skills=skills, lifecycle_skill=SkillLoadingPolicy.lifecycle_for_phase(phase), stage_skills=stage
    )


def read_only_task_message(phase: str) -> str:
    ticket = Ticket.objects.create(issue_url=f"https://example.com/issues/{Ticket.objects.count() + 5}")
    task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase=phase)
    facts = ReadOnlyFacts("issue", "", "", "", "", "a" * 40, REVIEW_DIFF, "green")
    return build_read_only_task_prompt(task, facts)
