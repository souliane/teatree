"""``suggest_skills`` must surface framework skills even with empty intent.

When the UserPromptSubmit hook (or a future PreToolUse hook) hands
``suggest_skills`` a payload with no prompt intent but a ``tool_input.file_path``
pointing at a teatree ``.py`` file, the framework-skill auto-detection
(``_framework_skills_for_directory``) must still fire on the file's directory
so ``/ac-django`` is suggested.

Pre-fix behaviour: ``suggest_skills`` short-circuits with
``return {"suggestions": [], "intent": ""}`` the moment intent is empty,
so Edit/Write on ``*.py`` under ``src/teatree/`` never triggers the loader.
"""

from __future__ import annotations  # noqa: TID251 — test for standalone script

import sys
from pathlib import Path

# scripts/lib lives outside src/; mirror the path injection used by
# tests/test_skill_loader.py.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib.skill_loader import suggest_skills  # noqa: E402


class TestNoIntentFilePathEmitsFrameworkSkills:
    """Empty intent + ``tool_input.file_path`` → framework skills surface."""

    def test_django_project_file_path_emits_ac_django(self, tmp_path: Path) -> None:
        # tmp_path acts as a fake "teatree" repo: manage.py at the root makes
        # the framework-skill detector classify it as Django.
        (tmp_path / "manage.py").touch()
        py_file = tmp_path / "src" / "teatree" / "core" / "models.py"
        py_file.parent.mkdir(parents=True)
        py_file.touch()

        result = suggest_skills(
            {
                "prompt": "",
                "cwd": str(tmp_path),
                "loaded_skills": [],
                "skill_search_dirs": [],
                "supplementary_config": "",
                "tool_input": {"file_path": str(py_file)},
            }
        )

        assert "ac-django" in result["suggestions"]

    def test_python_project_file_path_emits_ac_python(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'mypkg'\n", encoding="utf-8")
        py_file = tmp_path / "src" / "mypkg" / "module.py"
        py_file.parent.mkdir(parents=True)
        py_file.touch()

        result = suggest_skills(
            {
                "prompt": "",
                "cwd": str(tmp_path),
                "loaded_skills": [],
                "skill_search_dirs": [],
                "supplementary_config": "",
                "tool_input": {"file_path": str(py_file)},
            }
        )

        assert "ac-python" in result["suggestions"]

    def test_no_file_path_keeps_short_circuit(self, tmp_path: Path) -> None:
        # Without a file_path AND without intent, the old short-circuit still
        # applies: nothing to detect on.
        result = suggest_skills(
            {
                "prompt": "",
                "cwd": str(tmp_path),
                "loaded_skills": [],
                "skill_search_dirs": [],
                "supplementary_config": "",
            }
        )
        assert result["suggestions"] == []

    def test_loaded_framework_skill_not_re_suggested(self, tmp_path: Path) -> None:
        (tmp_path / "manage.py").touch()
        py_file = tmp_path / "src" / "teatree" / "x.py"
        py_file.parent.mkdir(parents=True)
        py_file.touch()

        result = suggest_skills(
            {
                "prompt": "",
                "cwd": str(tmp_path),
                "loaded_skills": ["ac-django"],
                "skill_search_dirs": [],
                "supplementary_config": "",
                "tool_input": {"file_path": str(py_file)},
            }
        )
        assert "ac-django" not in result["suggestions"]
