"""``t3 setup`` takes the networked pin measurement and records it for the doctor.

Real manifest, real git source, real ``git ls-remote`` — the setup step is the
only place the comparison touches the network, so these pin both halves of what
it owes: the operator sees the suggestion now, and the box keeps a record the
offline doctor can read later.
"""

from pathlib import Path

import pytest

from teatree.cli.setup.skill_pin_audit import SkillPinAuditor
from teatree.provisioning.skill_pin import read_pin_audit
from tests._git_repo import make_git_repo, run_git

_OWNER_REPO = "team/skills"
_SKILL = "ac-python"


@pytest.fixture
def source(tmp_path: Path) -> Path:
    origin = make_git_repo(tmp_path / _OWNER_REPO)
    (origin / _SKILL).mkdir()
    (origin / _SKILL / "SKILL.md").write_text("---\nname: ac-python\n---\n", encoding="utf-8")
    run_git(origin, "add", "-A")
    run_git(origin, "commit", "-q", "-m", "publish the skill")
    return origin


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    return project


def _declare(repo: Path, spec: str) -> None:
    (repo / "apm.yml").write_text(f"name: team/thing\ndependencies:\n  apm:\n  - {spec}\n", encoding="utf-8")


def _audit(repo: Path, tmp_path: Path) -> tuple[list[str], Path]:
    record = tmp_path / "record" / "audit.json"
    lines: list[str] = []
    SkillPinAuditor(repo, record, remote_base=f"{tmp_path}/").audit(lines.append)
    return lines, record


class TestSkillPinAuditor:
    def test_pin_the_source_moved_past_is_suggested_and_recorded(
        self, repo: Path, tmp_path: Path, source: Path
    ) -> None:
        pinned = run_git(source, "rev-parse", "HEAD")
        (source / _SKILL / "SKILL.md").write_text("---\nname: ac-python\n---\nfixed\n", encoding="utf-8")
        run_git(source, "commit", "-qam", "fix the skill")
        moved_to = run_git(source, "rev-parse", "HEAD")
        _declare(repo, f"{_OWNER_REPO}/{_SKILL}#{pinned}")

        lines, record = _audit(repo, tmp_path)

        assert any(line.startswith("INFO") and moved_to in line for line in lines)
        assert not any(line.startswith("FAIL") for line in lines)
        audit = read_pin_audit(record)
        assert audit is not None
        assert [status.head_sha for status in audit.statuses] == [moved_to]

    def test_pin_at_the_source_head_says_so_without_suggesting_a_bump(
        self, repo: Path, tmp_path: Path, source: Path
    ) -> None:
        _declare(repo, f"{_OWNER_REPO}/{_SKILL}#{run_git(source, 'rev-parse', 'HEAD')}")

        lines, record = _audit(repo, tmp_path)

        assert not any(line.startswith(("WARN", "FAIL")) for line in lines)
        assert "bump" not in " ".join(lines).lower()
        audit = read_pin_audit(record)
        assert audit is not None
        assert all(status.is_current for status in audit.statuses)

    def test_unreachable_source_is_recorded_unmeasurable_not_current(self, repo: Path, tmp_path: Path) -> None:
        _declare(repo, f"{_OWNER_REPO}/{_SKILL}#d0008a3")

        lines, record = _audit(repo, tmp_path)

        assert any(line.startswith("WARN") and "UNVERIFIED" in line for line in lines)
        audit = read_pin_audit(record)
        assert audit is not None
        [status] = audit.statuses
        assert status.unmeasurable
        assert not status.is_current

    def test_unreadable_declaration_surface_reports_itself_rather_than_going_silent(
        self, repo: Path, tmp_path: Path
    ) -> None:
        (repo / "apm.yml").write_text("name: team/thing\n", encoding="utf-8")

        lines, record = _audit(repo, tmp_path)

        assert any(line.startswith("WARN") and "UNVERIFIED" in line for line in lines)
        assert any("apm.yml" in line for line in lines)
        # Nothing was measured, so nothing may be recorded as measured.
        assert read_pin_audit(record) is None
