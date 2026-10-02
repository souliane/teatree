"""Tests for ``retire_alias`` — the ``t3 setup`` alias-retirement unit (#3232).

A ``PATH`` launcher reaches every shell, so the alias a previous ``t3 setup``
wrote is removed rather than refreshed. The rc files carry the operator's own
functions, so the assertion that matters is that everything outside the fenced
block survives byte for byte.
"""

from pathlib import Path

from teatree.cli.setup.docker_alias import retire_alias
from teatree.docker.workflow import ALIAS_MARKER_BEGIN, ALIAS_MARKER_END

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


def _rc_with_block(home: Path, name: str) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    rc = home / name
    rc.write_text(_ABOVE + _BLOCK + _BELOW, encoding="utf-8")
    return rc


class TestRetireAlias:
    def test_removes_the_block_from_every_rc_leaving_user_content_intact(self, tmp_path: Path) -> None:
        home = tmp_path / "rc-home"
        bashrc = _rc_with_block(home, ".bashrc")
        zshrc = _rc_with_block(home, ".zshrc")
        messages: list[str] = []

        retire_alias(echo=messages.append, home=home)

        assert bashrc.read_text(encoding="utf-8") == _ABOVE + _BELOW
        assert zshrc.read_text(encoding="utf-8") == _ABOVE + _BELOW
        assert [m for m in messages if m.startswith("OK")] == [
            f"OK    Removed the superseded containerized t3 alias from {rc} — the launcher replaces it."
            for rc in (bashrc, zshrc)
        ]

    def test_is_silent_when_no_rc_carries_the_block(self, tmp_path: Path) -> None:
        home = tmp_path / "rc-home"
        home.mkdir()
        (home / ".bashrc").write_text(_ABOVE, encoding="utf-8")
        messages: list[str] = []

        retire_alias(echo=messages.append, home=home)

        assert messages == []
        assert (home / ".bashrc").read_text(encoding="utf-8") == _ABOVE

    def test_never_creates_an_rc_file(self, tmp_path: Path) -> None:
        home = tmp_path / "rc-home"
        home.mkdir()
        retire_alias(echo=lambda _line: None, home=home)
        assert list(home.iterdir()) == []

    def test_an_unwritable_rc_warns_and_setup_continues(self, tmp_path: Path) -> None:
        home = tmp_path / "rc-home"
        home.mkdir()
        (home / ".bashrc").write_bytes(b"\xff\xfe not utf-8")
        messages: list[str] = []

        retire_alias(echo=messages.append, home=home)

        assert any(m.startswith("WARN") and "by hand" in m for m in messages)
