"""Enforcement guard for #932.

A django-typer ``@command`` subcommand that signals failure by *returning*
an error string leaves the process exiting 0 — django-typer serialises the
return value to stdout but never sets a non-zero code. CI / the loop /
headless callers then see success on a real failure (the false-completion
class). The fix is to ``self.stderr.write(...)`` then ``raise
SystemExit(1)`` (canonical: ``tasks.py``, ``db.py`` ``query``/``shell``).

This guard walks every ``core/management/commands`` module with AST and
fails if any ``@command``-decorated method has a ``return`` whose value is
a string literal / f-string mentioning "fail". It is deliberately narrow:

Scope (kept narrow so false positives stay low):

- Only ``@command`` methods are scanned, so module-level helpers like
    ``_workspace_cleanup.push_unsynced_branch`` keep returning "Push
    failed:" for their command (``clean-all``) to inspect and raise on.
- Only the word "fail" triggers it, so benign no-op returns such as
    "completed", "No X configured" or "Pushed:" never match.

If a genuinely-benign command must return a string containing "fail",
this guard should be made aware of it explicitly rather than relaxed
wholesale.
"""

import ast
from pathlib import Path

_COMMANDS_DIR = Path(__file__).resolve().parents[3] / "src" / "teatree" / "core" / "management" / "commands"


def _is_command_decorator(decorator: ast.expr) -> bool:
    """True for ``@command`` / ``@command(...)`` / ``@command(name=...)``."""
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    if isinstance(target, ast.Name):
        return target.id == "command"
    if isinstance(target, ast.Attribute):
        return target.attr == "command"
    return False


def _returned_string_value(node: ast.Return) -> str | None:
    """Return the static text of a returned string/f-string, else None."""
    value = node.value
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    if isinstance(value, ast.JoinedStr):
        return "".join(
            piece.value for piece in value.values if isinstance(piece, ast.Constant) and isinstance(piece.value, str)
        )
    return None


def _offending_returns(source: str) -> list[tuple[str, int, str]]:
    tree = ast.parse(source)
    offences: list[tuple[str, int, str]] = []
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef):
            continue
        if not any(_is_command_decorator(d) for d in func.decorator_list):
            continue
        for stmt in ast.walk(func):
            if not isinstance(stmt, ast.Return):
                continue
            text = _returned_string_value(stmt)
            if text and "fail" in text.lower():
                offences.append((func.name, stmt.lineno, text))
    return offences


class TestCommandFailureSignalling:
    def test_no_command_returns_a_failure_string(self) -> None:
        violations: list[str] = []
        for module in sorted(_COMMANDS_DIR.glob("*.py")):
            for func_name, lineno, text in _offending_returns(module.read_text()):
                violations.append(
                    f"{module.name}:{lineno} — @command `{func_name}` returns "
                    f"a failure string {text!r}; use `self.stderr.write(...)` "
                    f"then `raise SystemExit(1)` (see #932).",
                )
        assert not violations, "Commands must raise SystemExit(1) on failure, not return a string:\n" + "\n".join(
            violations,
        )
