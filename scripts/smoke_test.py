"""Smoke test for teatree installation.

Used by: t3-setup (Step 7).
Checks: hook parsing, statusline, Python imports, shell config.
"""

import subprocess
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(add_completion=False)
console = Console(stderr=True)


def _check(cmd: list[str], *, cwd: str | None = None) -> bool:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=10, check=False).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _check_python_imports(t3_repo: str) -> bool:
    code = f"import sys; sys.path.insert(0, '{t3_repo}/scripts'); from lib import registry, env, git"
    return _check([sys.executable, "-c", code])


@app.command()
def main(
    t3_repo: str = typer.Option("", envvar="T3_REPO", help="Path to teatree repo"),
) -> None:
    """Run smoke tests on the teatree installation."""
    if not t3_repo:
        console.print("[red]T3_REPO not set[/]")
        raise SystemExit(1)

    t3_path = Path(t3_repo)
    hook_script = t3_path / "integrations" / "claude-code-statusline" / "ensure-skills-loaded.sh"
    statusline_script = t3_path / "integrations" / "claude-code-statusline" / "statusline-command.sh"

    checks: list[tuple[str, bool]] = []

    if hook_script.is_file():
        checks.append(("Hook script parses (bash -n)", _check(["bash", "-n", str(hook_script)])))
    else:
        checks.append(("Hook script exists", False))

    if statusline_script.is_file():
        checks.append(("Statusline runs", _check([str(statusline_script)], cwd=str(t3_path))))
    else:
        checks.append(("Statusline script exists", False))

    checks.extend(
        [
            ("Python imports (lib.registry, lib.env, lib.git)", _check_python_imports(t3_repo)),
            ("t3 CLI responds", _check([sys.executable, str(t3_path / "scripts" / "t3_cli.py"), "--help"])),
            ("T3_REPO is a git repo", _check(["git", "-C", t3_repo, "rev-parse", "--git-dir"])),
        ]
    )

    shallow = subprocess.run(
        ["git", "-C", t3_repo, "rev-parse", "--is-shallow-repository"],
        capture_output=True,
        text=True,
        check=False,
    )
    checks.append(("Not a shallow clone", shallow.stdout.strip() == "false"))

    table = Table(title="Smoke Test Results")
    table.add_column("Check", style="bold")
    table.add_column("Result", justify="center")

    failures = 0
    for name, passed in checks:
        table.add_row(name, "[green]PASS[/]" if passed else "[red]FAIL[/]")
        if not passed:
            failures += 1

    console.print(table)
    console.print(
        f"\n[{'red' if failures else 'green'}]{failures} failure(s)[/]" if failures else "\n[green]All checks passed[/]"
    )

    if failures:
        raise SystemExit(1)
