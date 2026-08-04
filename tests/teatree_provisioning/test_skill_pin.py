"""Pin-vs-head comparison — the measurement behind the bump suggestion.

The sibling drift measurement answers "did my INSTALL leave the pin"; this one
answers "did the PIN leave its source", which nothing else asks. These exercise
the real thing: a real git repo as the source, a real ``git ls-remote`` against
it, and the mandate read from a real ``apm.yml`` — including the property the
whole check exists for, that a source it cannot reach is reported as unknown
rather than as agreement.
"""

from pathlib import Path

import pytest

from teatree.provisioning.declared import skills_declared_in_apm_manifest
from teatree.provisioning.skill_pin import measure_skill_pins, pin_advisory_lines
from tests._git_repo import make_git_repo, run_git

_SKILL = "ac-python"
_OWNER_REPO = "team/skills"


def _write_manifest(root: Path, entries: list[str]) -> Path:
    """A minimal ``apm.yml`` declaring *entries* on the mandated-skill surface."""
    manifest = root / "apm.yml"
    body = "\n".join(f"  - {entry}" for entry in entries)
    manifest.write_text(f"name: team/thing\ndependencies:\n  apm:\n{body}\n", encoding="utf-8")
    return manifest


@pytest.fixture
def source(tmp_path: Path) -> Path:
    """A source repo published at ``<remote base>team/skills``, one commit deep."""
    origin = make_git_repo(tmp_path / _OWNER_REPO)
    (origin / _SKILL).mkdir()
    (origin / _SKILL / "SKILL.md").write_text("---\nname: ac-python\n---\n", encoding="utf-8")
    run_git(origin, "add", "-A")
    run_git(origin, "commit", "-q", "-m", "publish the skill")
    return origin


def _remote_base(tmp_path: Path) -> str:
    """``measure_skill_pins`` joins this to ``<owner>/<repo>`` — a local dir here."""
    return f"{tmp_path}/"


def _measure(tmp_path: Path, pinned_at: str, *, base: str | None = None) -> list:
    manifest = _write_manifest(tmp_path, [f"{_OWNER_REPO}/{_SKILL}#{pinned_at}"])
    declared = skills_declared_in_apm_manifest(manifest)
    return measure_skill_pins(declared, remote_base=_remote_base(tmp_path) if base is None else base)


class TestMeasureSkillPins:
    def test_pin_at_the_source_head_suggests_nothing(self, tmp_path: Path, source: Path) -> None:
        head = run_git(source, "rev-parse", "HEAD")
        [status] = _measure(tmp_path, head)
        assert status.is_current
        assert not status.is_behind
        assert status.unmeasurable == ""
        assert pin_advisory_lines([status]) == []

    def test_pin_the_source_moved_past_names_the_sha_to_bump_to(self, tmp_path: Path, source: Path) -> None:
        # The defect: work merged after the pin was written is unreachable by
        # every consumer, and nothing anywhere is red about it.
        pinned = run_git(source, "rev-parse", "HEAD")
        (source / _SKILL / "SKILL.md").write_text("---\nname: ac-python\n---\nfixed\n", encoding="utf-8")
        run_git(source, "commit", "-qam", "fix the skill")
        moved_to = run_git(source, "rev-parse", "HEAD")

        [status] = _measure(tmp_path, pinned)

        assert status.is_behind
        assert not status.is_current
        assert status.head_sha == moved_to
        assert status.bumped_spec == f"{_OWNER_REPO}/{_SKILL}#{moved_to}"
        [line] = pin_advisory_lines([status])
        assert line.startswith("INFO")
        assert moved_to in line
        # Pasteable: the fix is a runnable command carrying the NEW sha.
        assert f"apm install {_OWNER_REPO}/{_SKILL}#{moved_to}" in line

    def test_unreachable_source_is_unmeasurable_never_up_to_date(self, tmp_path: Path) -> None:
        [status] = _measure(tmp_path, "d0008a3", base=f"{tmp_path / 'nowhere'}/")
        assert status.unmeasurable
        assert not status.is_current
        assert not status.is_behind
        [line] = pin_advisory_lines([status])
        assert line.startswith("WARN")
        assert "UNVERIFIED" in line
        assert "up to date" not in line.lower()

    def test_entry_carrying_no_pin_has_no_pin_to_measure(self, tmp_path: Path, source: Path) -> None:
        manifest = _write_manifest(tmp_path, [f"{_OWNER_REPO}/{_SKILL}"])
        declared = skills_declared_in_apm_manifest(manifest)
        assert measure_skill_pins(declared, remote_base=_remote_base(tmp_path)) == []
