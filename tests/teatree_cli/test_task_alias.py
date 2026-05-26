"""``t3 task complete <id>`` top-level alias (#1306).

Skill briefs reference `t3 task complete` as the short form for marking
a task done, but the actual command lives under the overlay's tasks
group (e.g. `t3 teatree tasks complete <id>`). Without the alias the
short form errored with `No such command 'task'.` and broke sub-agent
prompts that copy-pasted the documented form.

The alias forwards to `t3 <active-overlay> tasks <sub>` so the existing
overlay-scoped implementation owns the behaviour.
"""

from unittest.mock import patch

from typer.testing import CliRunner

from teatree.cli import app


class TestTaskAlias:
    def test_top_level_task_command_exists(self) -> None:
        runner = CliRunner()
        result = runner.invoke(app, ["task", "--help"])
        # The group exists (exit 0 on --help) — no "No such command 'task'".
        assert result.exit_code == 0
        assert "No such command" not in result.output

    def test_task_complete_forwards_to_overlay_tasks_complete(self) -> None:
        runner = CliRunner()
        captured: dict[str, object] = {}

        def fake_managepy(_project_path: object, *args: str, overlay_name: str = "") -> None:
            captured["args"] = args
            captured["overlay_name"] = overlay_name

        with patch("teatree.cli.task_alias.managepy", side_effect=fake_managepy):
            result = runner.invoke(app, ["task", "complete", "42", "--note", "shipped via !1234"])

        assert result.exit_code == 0, result.output
        # The alias forwards the args verbatim under `tasks complete`.
        args = captured["args"]
        assert isinstance(args, tuple)
        assert args[0] == "tasks"
        assert args[1] == "complete"
        assert "42" in args
        assert "--note" in args
