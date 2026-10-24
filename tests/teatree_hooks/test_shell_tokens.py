"""The public shell token surface preserves heredoc metadata for hooks."""

import subprocess
import sys
from pathlib import Path

from teatree.hooks.shell_tokens import (
    HeredocBody,
    TokenKind,
    live_substitution_starts,
    raw_substitution_sees_live,
    tokenize,
)


def test_public_shell_tokens_expose_heredoc_and_substitution_contract() -> None:
    bodies: list[HeredocBody] = []
    tokens = tokenize("cat << 'EOF'\nplain text\nEOF", heredoc_bodies=bodies)
    assert tokens[-1].kind is TokenKind.OP
    assert bodies[0].body == "plain text\n"
    assert bodies[0].quoted is True
    assert raw_substitution_sees_live('"$(echo hi)"', ("$(",)) is True


def test_live_substitution_starts_names_each_live_marker_and_skips_single_quoted_ones() -> None:
    raw = "echo '$(inert)' \"$(live)\" $(bare)"
    assert list(live_substitution_starts(raw, ("$(",))) == [raw.index("$(live"), raw.index("$(bare")]


def test_hooks_package_imports_without_lexer_in_minimal_tree(tmp_path: Path) -> None:
    package = tmp_path / "teatree"
    hooks = package / "hooks"
    hooks.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    source = Path(__file__).resolve().parents[2] / "src/teatree/hooks/__init__.py"
    (hooks / "__init__.py").write_text(source.read_text())
    result = subprocess.run(
        [sys.executable, "-S", "-c", "import teatree.hooks"],
        cwd=tmp_path,
        env={"PYTHONPATH": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
