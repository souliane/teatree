# test-path: cross-cutting — drives scripts/hooks/**/*.sh (no src mirror).
"""The shell hooks must run under the OLDEST bash their shebang can select.

``#!/usr/bin/env bash`` picks the first ``bash`` on PATH, and macOS ships 3.2 as
``/bin/bash`` — earlier than Homebrew's on a default PATH. A bash 4+ builtin is
therefore not a portability nicety here: it is absent at run time, and the
failure is silent in the worst way. ``mapfile`` in ``resolve_uv``'s candidate
collection left the candidate list EMPTY, so the banned-terms gate refused every
commit on the box while reporting a missing ``uv`` and recommending an install
that could not help — a fail-closed gate misdiagnosing its own crash.

Two assertions, because either alone is half a guarantee:

+ the SCAN pins the class on every host, including one whose only bash is 5.x
    and where the behavioural run below would prove nothing;
+ the RUN proves the real script actually resolves, under every bash the box
    has, which on macOS includes the 3.2 that the scan is written for.
"""

import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HOOK_SHELL = sorted((_REPO_ROOT / "scripts" / "hooks").rglob("*.sh"))
_LIB = _REPO_ROOT / "scripts" / "hooks" / "lib" / "resolve-uv.sh"

# Constructs bash 3.2 does not have. Each is a silent failure rather than a
# syntax error: the builtin is simply not found, and the surrounding script
# carries on with an empty result.
_BASH4_ONLY: dict[str, re.Pattern[str]] = {
    "mapfile": re.compile(r"^\s*mapfile\b", re.MULTILINE),
    "readarray": re.compile(r"^\s*readarray\b", re.MULTILINE),
    "associative array (declare -A)": re.compile(r"^\s*(?:local|declare|typeset)\s+-[A-Za-z]*A\b", re.MULTILINE),
    "case conversion (${v^^} / ${v,,})": re.compile(r"\$\{[A-Za-z_][A-Za-z_0-9]*(?:\^\^|,,)"),
}

# A stand-in for the compiled uv: no shebang, so `_uv_is_native` classifies it
# native. bash still runs it (execve reports ENOEXEC and bash falls back to sh).
_NATIVE_BODY = 'if [ "${1:-}" = "--version" ]; then echo "uv 0.0.0-native"; exit 0; fi\nexit 0\n'


def _every_bash() -> list[str]:
    """Every distinct bash this box can hand a ``#!/usr/bin/env bash`` script."""
    seen: dict[str, str] = {}
    for candidate in (shutil.which("bash"), "/bin/bash", "/usr/bin/bash", "/opt/homebrew/bin/bash"):
        if candidate and Path(candidate).exists():
            seen.setdefault(str(Path(candidate).resolve()), candidate)
    return sorted(seen.values())


def _code_lines(text: str) -> str:
    """The script with comment-only lines dropped, so prose about a builtin is not a use."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


class TestNoBash4OnlyConstructs:
    @pytest.mark.parametrize("script", _HOOK_SHELL, ids=lambda p: p.name)
    def test_hook_shell_stays_bash_3_2_compatible(self, script: Path) -> None:
        body = _code_lines(script.read_text(encoding="utf-8"))
        found = [name for name, pattern in _BASH4_ONLY.items() if pattern.search(body)]

        assert not found, (
            f"{script.relative_to(_REPO_ROOT)} uses bash 4+ only {found}; "
            "macOS selects /bin/bash 3.2 for these hooks, where that is absent and fails SILENTLY"
        )

    def test_the_scan_has_something_to_scan(self) -> None:
        # Anti-vacuity: an empty glob would make every case above pass forever.
        assert _LIB in _HOOK_SHELL


class TestResolverRunsUnderEveryBashOnTheBox:
    @pytest.mark.parametrize("bash", _every_bash())
    def test_resolve_uv_finds_the_planted_candidate(self, bash: str, tmp_path: Path) -> None:
        home = tmp_path / "home"
        native = home / ".pyenv" / "versions" / "3.13.1" / "bin" / "uv"
        native.parent.mkdir(parents=True)
        native.write_text(_NATIVE_BODY, encoding="utf-8")
        native.chmod(native.stat().st_mode | stat.S_IEXEC)

        result = subprocess.run(
            [bash, "-c", f'set -u; . "{_LIB}"; resolve_uv'],
            capture_output=True,
            text=True,
            check=False,
            env={
                "HOME": str(home),
                "XDG_CACHE_HOME": str(tmp_path / "cache"),
                "PYENV_ROOT": str(home / ".pyenv"),
                "ASDF_DATA_DIR": str(home / ".asdf"),
                "PATH": "/usr/bin:/bin",
            },
        )

        version = subprocess.run([bash, "--version"], capture_output=True, text=True, check=False).stdout
        assert result.returncode == 0, f"{bash} ({version.splitlines()[0]}) failed: {result.stderr!r}"
        assert result.stdout == str(native), f"{bash}: got {result.stdout!r}, stderr={result.stderr!r}"
