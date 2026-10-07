import logging
from pathlib import Path

import pytest

from teatree.skill_support import index
from teatree.skill_support.index import (
    build_skill_index,
    direct_requires,
    harness_skills_dirs,
    install_roots,
    resolve_skill_md,
    skill_mtimes,
)


def _skill(root: Path, name: str, *, requires: list[str] | None = None, companions: list[str] | None = None) -> Path:
    lines = ["---", f"name: {name}"]
    if requires is not None:
        lines += ["requires:", *(f"  - {dep}" for dep in requires)]
    if companions is not None:
        lines += ["companions:", *(f"  - {dep}" for dep in companions)]
    lines += ["---", f"# {name}", ""]
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def test_the_harness_roots_are_the_repo_skills_then_every_install_root() -> None:
    home = Path.home()
    assert harness_skills_dirs() == [
        index.DEFAULT_SKILLS_DIR,
        home / ".agents" / "skills",
        home / ".claude" / "skills",
        home / ".codex" / "skills",
    ]
    assert install_roots() == harness_skills_dirs()[1:]


def test_harness_roots_dedupe_a_repo_dir_that_is_also_an_install_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(index, "DEFAULT_SKILLS_DIR", Path.home() / ".claude" / "skills")
    assert harness_skills_dirs().count(Path.home() / ".claude" / "skills") == 1


def test_a_skill_only_in_an_install_root_is_indexed_with_its_requires(tmp_path: Path) -> None:
    local, installed = tmp_path / "local", tmp_path / "installed"
    _skill(local, "alpha", requires=["beta"])
    _skill(installed, "beta", requires=["gamma"], companions=["delta"])

    entries = {entry["skill"]: entry for entry in build_skill_index([local, installed])}

    assert entries["alpha"]["requires"] == ["beta"]
    assert entries["beta"] == {"skill": "beta", "requires": ["gamma"], "companions": ["delta"]}


def test_the_index_and_the_resolver_agree_on_which_root_wins(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    winner = _skill(first, "alpha", requires=["from-first"])
    _skill(second, "alpha", requires=["from-second"])

    (entry,) = build_skill_index([first, second])

    assert resolve_skill_md("alpha", [first, second]) == winner
    assert entry["requires"] == ["from-first"]


def test_the_index_skips_dirs_without_a_skill_md_and_missing_roots(tmp_path: Path) -> None:
    (tmp_path / "root" / "not-a-skill").mkdir(parents=True)
    _skill(tmp_path / "root", "real")

    assert [e["skill"] for e in build_skill_index([tmp_path / "absent", tmp_path / "root"])] == ["real"]


def test_the_resolver_accepts_qualified_and_path_references(tmp_path: Path) -> None:
    path = _skill(tmp_path, "alpha")

    assert resolve_skill_md("t3:alpha", [tmp_path]) == path
    assert resolve_skill_md("/elsewhere/skills/alpha/SKILL.md", [tmp_path]) == path
    assert resolve_skill_md("ghost", [tmp_path]) is None


def test_direct_requires_reads_only_the_first_level(tmp_path: Path) -> None:
    _skill(tmp_path, "alpha", requires=["beta"])
    _skill(tmp_path, "beta", requires=["gamma"])

    assert direct_requires("alpha", [tmp_path]) == ["beta"]
    assert direct_requires("ghost", [tmp_path]) == []


def _latin1_skill(root: Path, name: str) -> Path:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(f"---\nname: {name}\nrequires:\n  - beta\n---\n# caf\xe9\n".encode("latin-1"))
    return path


def test_a_non_utf8_skill_md_is_skipped_and_named_instead_of_failing_the_index(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _skill(tmp_path, "alpha", requires=["beta"])
    latin = _latin1_skill(tmp_path, "latin")

    with caplog.at_level(logging.WARNING, logger=index.__name__):
        entries = build_skill_index([tmp_path])

    assert [entry["skill"] for entry in entries] == ["alpha"]
    assert str(latin) in caplog.text


def test_direct_requires_of_a_non_utf8_skill_md_is_empty_and_named(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    latin = _latin1_skill(tmp_path, "latin")

    with caplog.at_level(logging.WARNING, logger=index.__name__):
        assert direct_requires("latin", [tmp_path]) == []

    assert str(latin) in caplog.text


def test_skill_mtimes_cover_every_root_and_see_a_removal(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    _skill(first, "alpha")
    gone = _skill(second, "beta")

    before = skill_mtimes([first, second])
    gone.unlink()

    assert set(before) == {str(first / "alpha" / "SKILL.md"), str(second / "beta" / "SKILL.md")}
    assert skill_mtimes([first, second]) != before
