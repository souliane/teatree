r"""The skill-loading gate fires only on code work, never on docs/config/git/ask.

The PreToolUse skill-loading gate (``handle_enforce_skill_loading``) used to
fire on EVERY ``Bash``/``Edit``/``Write`` once a Python/Django skill landed in
``<session>.pending`` — and, because the router runs every PreToolUse handler
regardless of which matcher triggered, it also fired on ``AskUserQuestion``.

That over-fired on non-Python work: editing a ``.md`` / ``.yml`` / ``.sh`` file,
a ``git status`` / dotfiles commit, or asking the user a question got
hard-blocked demanding ``/ac-python`` / ``/ac-django`` — with no usable escape
on ``Edit``/``Write``/``AskUserQuestion``. Per the doctrine "a gate whose
heuristic can't cleanly separate bad from legit must WARN or be tightly
scoped, never hard-FAIL on the ambiguous case", the gate is now scoped to fire
ONLY when the tool call is genuinely code-touching:

* ``Edit`` / ``Write`` whose ``file_path`` is a Python/Django source file;
* ``Bash`` whose command runs Python tooling (python, uv run, pytest, ruff, ty);

and NEVER on ``AskUserQuestion`` (or any non-code tool), nor on a markdown /
yaml / toml / shell edit, nor on a pure-git / non-Python Bash command.

Integration-style: the real handler, real ``STATE_DIR`` on ``tmp_path``, a real
resolvable-but-unloaded skill seeded under the temp search dir, so a code-work
call still hard-blocks (the gate keeps its teeth) while non-code work passes.
"""

import json
from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import _skill_gate_targets_code_work, handle_enforce_skill_loading


def _seed_skill(skills_dir: Path, name: str) -> None:
    skill = skills_dir / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")


@pytest.fixture
def gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    original_state = router.STATE_DIR
    router.STATE_DIR = tmp_path / "state"
    router.STATE_DIR.mkdir(parents=True, exist_ok=True)

    skills_dir = tmp_path / "skills"
    _seed_skill(skills_dir, "ac-python")
    monkeypatch.setenv("T3_SKILL_SEARCH_DIRS", str(skills_dir))

    yield skills_dir

    router.STATE_DIR = original_state


def _write_pending(session_id: str, skills: list[str]) -> None:
    (router.STATE_DIR / f"{session_id}.pending").write_text("\n".join(skills) + "\n", encoding="utf-8")


def _write_loop_pending(session_id: str) -> None:
    (router.STATE_DIR / f"{session_id}.loop-pending").write_text("1", encoding="utf-8")


def _run(data: dict) -> tuple[bool, dict | None]:
    out = StringIO()
    err = StringIO()
    with patch("sys.stdout", out), patch("sys.stderr", err):
        blocked = handle_enforce_skill_loading(data)
    payload = json.loads(out.getvalue()) if out.getvalue().strip() else None
    return blocked, payload


def _py_fixture(tmp_path: Path) -> str:
    """A code-work ``.py`` fixture path anchored under ``tmp_path``.

    The skill-loading gate keys only on the source ``.py`` shape, so any ``.py``
    path drives it. The path must live OUTSIDE any teatree-managed repo: a
    repo-relative ``src/teatree/...`` literal would, on a checkout sitting on
    ``main`` (the push-to-main CI ``test`` job's cwd), trip the higher-priority
    ``handle_protect_default_branch`` SAFETY gate first and preempt the
    skill-loading deny this suite drives (souliane/teatree#2003).
    """
    return str(tmp_path / "work" / "x.py")


class TestScopePredicate:
    """``_skill_gate_targets_code_work`` separates code work from everything else."""

    @pytest.mark.parametrize(
        "file_path",
        ["src/teatree/core/models.py", "tests/test_x.py", "a/b/c.pyi"],
    )
    def test_python_edit_is_code_work(self, file_path: str) -> None:
        assert _skill_gate_targets_code_work({"tool_name": "Edit", "tool_input": {"file_path": file_path}}) is True

    @pytest.mark.parametrize(
        "file_path",
        ["README.md", "config.yml", "config.yaml", "pyproject.toml", "setup.sh", ".teatree.toml", "notes.txt"],
    )
    def test_non_python_edit_is_not_code_work(self, file_path: str) -> None:
        for tool in ("Edit", "Write"):
            assert (
                _skill_gate_targets_code_work({"tool_name": tool, "tool_input": {"file_path": file_path}}) is False
            ), f"{tool} {file_path} must not be code work"

    @pytest.mark.parametrize(
        "command",
        [
            "uv run pytest -q",
            "python manage.py migrate",
            "python3 -m pytest",
            "ruff check src/",
            "uv run ty check",
            "pytest tests/test_x.py",
        ],
    )
    def test_python_tooling_bash_is_code_work(self, command: str) -> None:
        assert _skill_gate_targets_code_work({"tool_name": "Bash", "tool_input": {"command": command}}) is True

    @pytest.mark.parametrize(
        "command",
        [
            "git status",
            "git commit -m 'docs: update'",
            "ls -la",
            "cat README.md",
            "grep -r foo .",
            "markdownlint docs/",
        ],
    )
    def test_non_python_bash_is_not_code_work(self, command: str) -> None:
        assert _skill_gate_targets_code_work({"tool_name": "Bash", "tool_input": {"command": command}}) is False

    def test_ask_user_question_is_never_code_work(self) -> None:
        assert _skill_gate_targets_code_work({"tool_name": "AskUserQuestion", "tool_input": {}}) is False


class TestGateNeverFiresOnNonCodeWork:
    """With a real unloaded Python skill pending, non-code calls pass; code calls block."""

    def test_ask_user_question_never_blocked(self, gate: Path) -> None:
        _write_pending("sess-ask", ["ac-python"])
        blocked, payload = _run(
            {"session_id": "sess-ask", "tool_name": "AskUserQuestion", "tool_input": {"questions": []}}
        )
        assert blocked is False
        assert payload is None

    @pytest.mark.parametrize("file_path", ["README.md", "skills/code/SKILL.md", "ci.yml", "deploy.sh"])
    def test_doc_config_shell_edit_passes(self, gate: Path, file_path: str) -> None:
        _write_pending("sess-doc", ["ac-python"])
        blocked, payload = _run({"session_id": "sess-doc", "tool_name": "Edit", "tool_input": {"file_path": file_path}})
        assert blocked is False
        assert payload is None

    def test_git_bash_passes(self, gate: Path) -> None:
        _write_pending("sess-git", ["ac-python"])
        blocked, payload = _run(
            {"session_id": "sess-git", "tool_name": "Bash", "tool_input": {"command": "git commit -m 'wip'"}}
        )
        assert blocked is False
        assert payload is None


class TestGateStillFiresOnCodeWork:
    """The narrowing must not defang the gate on genuine Python/Django work."""

    def test_python_edit_still_blocks(self, gate: Path, tmp_path: Path) -> None:
        _write_pending("sess-py", ["ac-python"])
        blocked, payload = _run(
            {"session_id": "sess-py", "tool_name": "Edit", "tool_input": {"file_path": _py_fixture(tmp_path)}}
        )
        assert blocked is True
        assert payload is not None
        assert payload["permissionDecision"] == "deny"
        assert "ac-python" in payload["permissionDecisionReason"]

    def test_python_tooling_bash_still_blocks(self, gate: Path) -> None:
        _write_pending("sess-pytest", ["ac-python"])
        blocked, payload = _run(
            {"session_id": "sess-pytest", "tool_name": "Bash", "tool_input": {"command": "uv run pytest -q"}}
        )
        assert blocked is True
        assert payload is not None
        assert payload["permissionDecision"] == "deny"

    def test_code_work_still_honors_skill_load_ok_escape(self, gate: Path, tmp_path: Path) -> None:
        _write_pending("sess-esc", ["ac-python"])
        blocked, payload = _run(
            {
                "session_id": "sess-esc",
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": _py_fixture(tmp_path),
                    "new_string": "x = 1  # [skill-load-ok: false trigger]",
                },
            }
        )
        assert blocked is False
        assert payload is None


class TestLoopBootstrapExemption:
    """The skill-load gate must not deadlock a loop-registration bootstrap turn (#1918)."""

    def test_loop_bootstrap_turn_not_blocked(self, gate: Path) -> None:
        # A code-work Bash with a resolvable unloaded skill pending would normally
        # block; the loop-pending marker (this session is mid loop-bootstrap) exempts it.
        _write_pending("sess-loop", ["ac-python"])
        _write_loop_pending("sess-loop")
        blocked, payload = _run(
            {"session_id": "sess-loop", "tool_name": "Bash", "tool_input": {"command": "uv run pytest -q"}}
        )
        assert blocked is False, (
            "DEADLOCK regression (#1918) — code work during a loop-registration bootstrap turn was blocked."
        )
        assert payload is None

    def test_gate_still_fires_after_loop_registers(self, gate: Path) -> None:
        # No loop-pending marker (the bootstrap turn cleared it once the loop
        # registered) → the gate keeps its teeth on genuine code work.
        _write_pending("sess-registered", ["ac-python"])
        blocked, payload = _run(
            {"session_id": "sess-registered", "tool_name": "Bash", "tool_input": {"command": "uv run pytest -q"}}
        )
        assert blocked is True
        assert payload is not None
        assert payload["permissionDecision"] == "deny"
