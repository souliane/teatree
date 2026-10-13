"""README loop inventory follows the shipped seed table."""

from pathlib import Path

from scripts.hooks import update_readme_skills
from teatree.loops.seed import DEFAULT_LOOPS

ROOT = Path(__file__).resolve().parents[3]


def test_readme_loop_inventory_matches_seed() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    begin = "<!-- BEGIN LOOPS -->"
    end = "<!-- END LOOPS -->"
    assert begin in readme
    assert end in readme
    block = readme.split(begin, 1)[1].split(end, 1)[0]
    assert {line.split("`", 2)[1] for line in block.splitlines() if line.startswith("| `")} == {
        spec.name for spec in DEFAULT_LOOPS
    }


def test_generator_repairs_drift_and_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    readme = tmp_path / "README.md"
    original = (ROOT / "README.md").read_text(encoding="utf-8")
    readme.write_text(original.replace("| `inbox` |", "| `old-loop` |"), encoding="utf-8")
    monkeypatch.setattr(update_readme_skills, "README_PATH", readme)
    assert update_readme_skills.main() == 1
    assert readme.read_text(encoding="utf-8") == original
    assert update_readme_skills.main() == 0


def test_ci_checks_readme_generation() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "- run: uv run python scripts/hooks/update_readme_skills.py" in workflow


def test_generator_reports_missing_file(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(update_readme_skills, "README_PATH", tmp_path / "missing.md")
    assert update_readme_skills.main() == 1
    assert "not found" in capsys.readouterr().err


def test_generator_reports_missing_markers(tmp_path: Path, monkeypatch, capsys) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("no markers", encoding="utf-8")
    monkeypatch.setattr(update_readme_skills, "README_PATH", readme)
    assert update_readme_skills.main() == 1
    assert "missing generation markers" in capsys.readouterr().err
