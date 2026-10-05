import pytest

from teatree.hooks.read_only_command import is_read_only


@pytest.mark.parametrize(
    "command",
    [
        (
            "cd ~/.claude/skills/project-status && wc -l references/current-state.md "
            "&& sed -n '1,60p' references/current-state.md && git log -1"
        ),
        "git -C /repo log --oneline -5 2>&1 | head -3",
        "rg -n 'visible_plan' src/ 2>/dev/null",
        "find . -maxdepth 2 -name '*.py'",
        "ls -la && pwd",
        "FOO=1 grep -c needle file.txt",
        "git fetch origin main -q && git diff origin/main --stat",
        "cat 'notes $(not run).md'",
    ],
    ids=[
        "status_probe",
        "git_log_piped",
        "search_to_dev_null",
        "find_by_name",
        "listing",
        "env_prefixed",
        "fetch_and_diff",
        "single_quoted_substitution",
    ],
)
def test_inspections_are_read_only(command: str) -> None:
    assert is_read_only(command) is True


@pytest.mark.parametrize(
    "command",
    [
        "",
        "git commit -m wip",
        "git push origin HEAD",
        "git -C /repo checkout -b feature",
        "sed -i 's/a/b/' notes.md",
        "sed -n '1w output.txt' input.txt",
        "rg --pre ./decode.sh needle",
        "git -c core.pager=./page.sh log",
        "cat notes.md > copy.md",
        'cat "$(rm -rf build)"',
        "find . -name '*.pyc' -delete",
        "find . -fprintf output.txt '%p\\n'",
        "git log --output=log.txt",
        "ls | xargs rm",
        "rm -rf build",
        'echo "dispatch placeholder"',
        "uv run pytest",
    ],
    ids=[
        "empty",
        "commit",
        "push",
        "branch_switch",
        "sed_in_place",
        "sed_script_write",
        "rg_preprocessor",
        "git_config_override",
        "redirect_write",
        "live_substitution",
        "find_delete",
        "find_fprintf",
        "git_output_file",
        "piped_into_writer",
        "rm",
        "placeholder_echo",
        "test_run",
    ],
)
def test_anything_that_acts_is_not_read_only(command: str) -> None:
    assert is_read_only(command) is False
