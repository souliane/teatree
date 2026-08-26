"""``deploy/t3`` retires the superseded managed alias block from the HOST rc files.

The alias only ever reached interactive shells, so a ``PATH`` launcher supersedes
it — and while both existed they could name different checkouts, which is the
split-brain worth removing. The removal has to live in the wrapper because the
container that now runs ``t3 setup`` cannot see the operator's rc files, and the
wrapper is the only layer that executes on the host.

Nothing links the wrapper's marker strings to
:mod:`teatree.docker.workflow`'s, so this evaluates the wrapper's own function
under bash and pins both the markers and the byte-level result.
"""

# test-path: cross-cutting -- pins deploy/t3's bash retirement against src/teatree/docker/workflow.py

import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from teatree.docker.workflow import ALIAS_MARKER_BEGIN, ALIAS_MARKER_END, AliasRemoval, remove_alias_block

WRAPPER = Path(__file__).resolve().parents[1] / "deploy" / "t3"
BASH = shutil.which("bash") or "/bin/bash"

#: The wrapper's retirement unit: the two marker assignments through the close of
#: the function that consumes them.
RETIREMENT_BLOCK = re.compile(
    r"^ALIAS_MARKER_BEGIN=.*?^retire_managed_alias_block\(\) \{.*?^\}", re.DOTALL | re.MULTILINE
)

_ABOVE = """# the operator's own profile
greet() {
    echo hello
}

"""
_BELOW = """export EDITOR=emacs
farewell() {
    echo bye
}
"""
_BLOCK = f'{ALIAS_MARKER_BEGIN}\nalias t3="/somewhere/deploy/t3"\n{ALIAS_MARKER_END}\n'


def _retire(home: Path) -> subprocess.CompletedProcess[str]:
    """Run the wrapper's own retirement function against rc files under *home*."""
    block = RETIREMENT_BLOCK.search(WRAPPER.read_text(encoding="utf-8"))
    assert block is not None, f"{WRAPPER} no longer declares retire_managed_alias_block"
    script = (
        f"set -euo pipefail\nTEATREE_HOST_HOME={shlex.quote(str(home))}\n{block.group(0)}\nretire_managed_alias_block"
    )
    return subprocess.run([BASH, "-c", script], capture_output=True, text=True, check=True, timeout=30)


@pytest.fixture
def home_with_alias(tmp_path: Path) -> Path:
    home = tmp_path / "rc-home"
    home.mkdir()
    for name in (".bashrc", ".zshrc"):
        (home / name).write_text(_ABOVE + _BLOCK + _BELOW, encoding="utf-8")
    return home


class TestTheWrapperRetiresTheAliasOnTheHost:
    def test_only_the_fenced_block_goes(self, home_with_alias: Path) -> None:
        _retire(home_with_alias)
        for name in (".bashrc", ".zshrc"):
            assert (home_with_alias / name).read_text(encoding="utf-8") == _ABOVE + _BELOW

    def test_is_idempotent(self, home_with_alias: Path) -> None:
        _retire(home_with_alias)
        after_first = (home_with_alias / ".zshrc").read_bytes()
        _retire(home_with_alias)
        assert (home_with_alias / ".zshrc").read_bytes() == after_first

    def test_an_rc_without_the_block_is_untouched(self, tmp_path: Path) -> None:
        home = tmp_path / "rc-home"
        home.mkdir()
        (home / ".bashrc").write_text(_ABOVE, encoding="utf-8")
        _retire(home)
        assert (home / ".bashrc").read_text(encoding="utf-8") == _ABOVE

    def test_never_creates_an_rc_file(self, tmp_path: Path) -> None:
        home = tmp_path / "rc-home"
        home.mkdir()
        _retire(home)
        assert list(home.iterdir()) == []

    def test_writes_through_a_symlinked_rc_rather_than_replacing_it(self, tmp_path: Path) -> None:
        home = tmp_path / "rc-home"
        home.mkdir()
        real = tmp_path / "dotfiles" / "zshrc"
        real.parent.mkdir()
        real.write_text(_ABOVE + _BLOCK + _BELOW, encoding="utf-8")
        (home / ".zshrc").symlink_to(real)

        _retire(home)
        assert (home / ".zshrc").is_symlink()
        assert real.read_text(encoding="utf-8") == _ABOVE + _BELOW


class TestTheTwoImplementationsAgree:
    """The wrapper and :func:`remove_alias_block` are one behaviour in two languages."""

    def test_the_wrapper_declares_the_same_markers_python_does(self) -> None:
        wrapper = WRAPPER.read_text(encoding="utf-8")
        assert f"ALIAS_MARKER_BEGIN='{ALIAS_MARKER_BEGIN}'" in wrapper
        assert f"ALIAS_MARKER_END='{ALIAS_MARKER_END}'" in wrapper

    def test_both_produce_the_same_bytes_from_the_same_rc(self, tmp_path: Path) -> None:
        source = _ABOVE + _BLOCK + _BELOW
        via_bash = tmp_path / "rc-home"
        via_bash.mkdir()
        (via_bash / ".bashrc").write_text(source, encoding="utf-8")
        _retire(via_bash)

        via_python = tmp_path / "python-rc"
        via_python.write_text(source, encoding="utf-8")
        assert remove_alias_block(via_python) is AliasRemoval.REMOVED

        assert (via_bash / ".bashrc").read_bytes() == via_python.read_bytes()


class TestRetirementIsScopedToSetup:
    def test_only_the_setup_subcommand_triggers_it(self) -> None:
        # Every `t3` call would otherwise rewrite the operator's rc files.
        wrapper = WRAPPER.read_text(encoding="utf-8")
        assert 'if [ "${1:-}" = setup ]; then\n    retire_managed_alias_block\nfi' in wrapper


class TestAnUnterminatedBlockIsRefusedNotTruncated:
    """An rc file is the operator's own, and this is the only code that rewrites it.

    A BEGIN whose END is missing — a half-applied edit, a truncated write, a
    hand-deleted closing line — used to drop every line from the marker to EOF, in
    place, with nothing to recover from.
    """

    @pytest.fixture
    def home_with_unterminated_block(self, tmp_path: Path) -> Path:
        home = tmp_path / "rc-home"
        home.mkdir()
        for name in (".bashrc", ".zshrc"):
            (home / name).write_text(
                _ABOVE + f'{ALIAS_MARKER_BEGIN}\nalias t3="/somewhere/deploy/t3"\n' + _BELOW, encoding="utf-8"
            )
        return home

    def test_the_file_is_left_exactly_as_it_was(self, home_with_unterminated_block: Path) -> None:
        before = {
            name: (home_with_unterminated_block / name).read_text(encoding="utf-8") for name in (".bashrc", ".zshrc")
        }

        _retire(home_with_unterminated_block)

        for name, content in before.items():
            assert (home_with_unterminated_block / name).read_text(encoding="utf-8") == content

    def test_the_operator_is_told_which_file_to_repair(self, home_with_unterminated_block: Path) -> None:
        result = _retire(home_with_unterminated_block)

        assert ".bashrc" in result.stderr
        assert ALIAS_MARKER_END in result.stderr
