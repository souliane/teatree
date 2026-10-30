"""The coordinating skill holds the shared duties; the interactive skill refers to it and keeps only its own delta.

``coordinating`` runs inside the factory on whatever harness the coordinator runs on, so it
names no harness and no attended session. ``interactive`` requires it, links it and states
only what an attended session adds. These tests pin the wiring, that no duty text is written
twice, that no section of the original interactive skill was lost, and the size ceilings.
"""

import re
from pathlib import Path

import pytest

from teatree.agents.phase_agent_skills import declared_skills_for_phase
from teatree.agents.skill_bundle import resolve_skill_bundle
from teatree.agents.skill_injection import _read_skill_contents_scoped
from teatree.skill_support.deps import resolve_requires
from teatree.skill_support.index import build_skill_index
from teatree.types import SkillMetadata

_ROOT = Path(__file__).resolve().parents[2]
_SKILLS = _ROOT / "skills"
_COORDINATING = _SKILLS / "coordinating"
_INTERACTIVE = _SKILLS / "interactive"

_INTERACTIVE_MAX_BYTES = 18_432
_COORDINATING_MAX_BYTES = 16_384

#: A sentence of this many normalized words written in both skills is a duty stated twice.
_DUPLICATE_SENTENCE_WORDS = 8

_HARNESS_TOKENS = ("claude", "codex", "pydantic", "listagents", "sessionstart", "attended", "interactive")

#: Every heading of skills/interactive/SKILL.md at 6385a5d51, before the split, and where its text lives now.
_ORIGINAL_HEADINGS: dict[str, tuple[Path, str]] = {
    "# TeaTree — Interactive Session": (_INTERACTIVE, "# TeaTree — Interactive Session"),
    "## Resolve settled owner cards and agent asks": (_COORDINATING, "## Resolve settled owner cards and agent asks"),
    "## This session does not implement — it files and enqueues (Non-Negotiable)": (
        _COORDINATING,
        "## File and enqueue — never implement (Non-Negotiable)",
    ),
    "## And it does not investigate inline — a sub-agent does (Non-Negotiable)": (
        _INTERACTIVE,
        "## And it does not investigate inline — a sub-agent does (Non-Negotiable)",
    ),
    "## If it does code, it plans first (Non-Negotiable)": (
        _INTERACTIVE,
        "## If it does code, it plans first (Non-Negotiable)",
    ),
    "## No work-bearing state is terminal": (_INTERACTIVE, "## No work-bearing state is terminal"),
    "## A status field is a report, not the state": (_COORDINATING, "## A status field is a report, not the state"),
    "## Factory watch — BLOCKING first (Non-Negotiable)": (
        _COORDINATING,
        "## Factory watch — BLOCKING first (Non-Negotiable)",
    ),
    "## Skill Loading": (_INTERACTIVE, "## Skill Loading"),
    "## Standing directives": (_INTERACTIVE, "## Standing directives"),
    "## Reach a running factory agent": (_INTERACTIVE, "## Reach a running factory agent"),
    "## Claude-only hook automation": (_INTERACTIVE, "## Claude-only hook automation"),
    "## Interactive vs Headless Output": (_INTERACTIVE, "## Interactive vs Headless Output"),
    "## Related Skills": (_INTERACTIVE, "## Related Skills"),
}

_NEW_SHARED_DUTY_HEADINGS = (
    "## Mailbox, both ways",
    "## Drain stranded work",
    "## Unblock — review, merge, advance the board",
)

_REFERENCES = {
    "skills/coordinating/references/factory-watch.md",
    "skills/coordinating/references/stranded-work.md",
}

_HEADING_LINE = re.compile(r"^#{1,6} .+$", re.MULTILINE)
_SENTENCE_BREAK = re.compile(r"(?<=[.!?:;])\s+|\n+|\|")
_NON_WORD = re.compile(r"[^a-z0-9\s]")


def _text(skill_dir: Path) -> str:
    return (skill_dir / "SKILL.md").read_text(encoding="utf-8")


def _coordinating_texts() -> dict[str, str]:
    return {
        str(path.relative_to(_ROOT)): path.read_text(encoding="utf-8") for path in sorted(_COORDINATING.rglob("*.md"))
    }


def _headings(text: str) -> list[str]:
    return _HEADING_LINE.findall(text)


def _long_sentences(text: str) -> set[str]:
    without_fences = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    normalized = (" ".join(_NON_WORD.sub(" ", raw.lower()).split()) for raw in _SENTENCE_BREAK.split(without_fences))
    return {sentence for sentence in normalized if len(sentence.split()) >= _DUPLICATE_SENTENCE_WORDS}


@pytest.fixture(scope="module")
def skill_index() -> list[dict[str, object]]:
    return build_skill_index([_SKILLS])


def test_the_coordinator_brief_declares_and_lists_coordinating(skill_index: list[dict[str, object]]) -> None:
    assert "coordinating" in declared_skills_for_phase("answering")
    bundle = resolve_skill_bundle(
        phase="answering",
        overlay_skill_metadata=SkillMetadata(),
        skill_index=skill_index,
        detection_root=_ROOT,
        stage_skills=[],
    )
    assert "coordinating" in bundle
    brief = _read_skill_contents_scoped(bundle, primary_skills={"answerer"}, skills_dir=_SKILLS)
    assert "- coordinating: not embedded" in brief


def test_the_coordinator_declared_skills_never_depend_on_the_interactive_skill(
    skill_index: list[dict[str, object]],
) -> None:
    unfiltered = resolve_requires(declared_skills_for_phase("answering"), skill_index)
    assert "coordinating" in unfiltered
    assert "interactive" not in unfiltered


def test_the_coordinating_closure_never_reaches_the_interactive_skill(skill_index: list[dict[str, object]]) -> None:
    closure = resolve_requires(["coordinating"], skill_index)
    assert closure == ["rules", "coordinating"]


def test_the_interactive_skill_requires_and_links_the_coordinating_skill(skill_index: list[dict[str, object]]) -> None:
    assert resolve_requires(["interactive"], skill_index) == ["rules", "coordinating", "interactive"]
    assert "(../coordinating/SKILL.md)" in _text(_INTERACTIVE)


def test_no_duty_sentence_is_written_in_both_skills() -> None:
    interactive = _long_sentences(_text(_INTERACTIVE))
    coordinating = [_long_sentences(body) for body in _coordinating_texts().values()]
    assert interactive
    assert all(coordinating)
    twice = {sentence for sentences in coordinating for sentence in sentences & interactive}
    assert not twice, "sentences present in both skills:\n" + "\n".join(sorted(twice))


@pytest.mark.parametrize(("original", "home"), _ORIGINAL_HEADINGS.items())
def test_each_original_section_has_exactly_one_home(original: str, home: tuple[Path, str]) -> None:
    skill_dir, heading = home
    homes = [d for d in (_INTERACTIVE, _COORDINATING) if heading in _headings(_text(d))]
    assert homes == [skill_dir], f"{original!r} -> {heading!r} found in {homes}"


@pytest.mark.parametrize("heading", _NEW_SHARED_DUTY_HEADINGS)
def test_a_new_shared_duty_heading_exists_only_in_the_coordinating_skill(heading: str) -> None:
    assert heading in _headings(_text(_COORDINATING))
    assert heading not in _headings(_text(_INTERACTIVE))


def test_the_coordinating_skill_names_its_references_and_the_old_path_is_gone() -> None:
    named = set(re.findall(r"skills/coordinating/references/[\w.-]+\.md", _text(_COORDINATING)))
    assert named == _REFERENCES
    assert not [path for path in named if not (_ROOT / path).is_file()]
    assert not (_INTERACTIVE / "references").exists()


def test_the_coordinating_skill_names_no_harness_and_no_attended_session() -> None:
    offenders = {
        name: [token for token in _HARNESS_TOKENS if token in body.lower()]
        for name, body in _coordinating_texts().items()
    }
    assert _coordinating_texts()
    assert not {name: tokens for name, tokens in offenders.items() if tokens}


def test_the_interactive_skill_is_clearly_smaller_than_it_was() -> None:
    assert len(_text(_INTERACTIVE).encode()) <= _INTERACTIVE_MAX_BYTES


def test_the_coordinating_skill_fits_the_skill_budget() -> None:
    assert len(_text(_COORDINATING).encode()) <= _COORDINATING_MAX_BYTES
