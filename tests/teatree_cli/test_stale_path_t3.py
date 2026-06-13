"""Tests for ``_check_stale_path_t3`` — detect a shadowing on-PATH ``t3``."""

import io
from contextlib import redirect_stdout
from pathlib import Path

from teatree.cli._doctor_checks import _check_stale_path_t3


def _run(env_path: str, home: Path, *, uv_tool_bin_dir: str | None = None) -> tuple[bool, str]:
    out = io.StringIO()
    env: dict[str, str] = {"PATH": env_path, "HOME": str(home)}
    if uv_tool_bin_dir is not None:
        env["UV_TOOL_BIN_DIR"] = uv_tool_bin_dir
    with redirect_stdout(out):
        ok = _check_stale_path_t3(env=env)
    return ok, out.getvalue()


class TestCheckStalePathT3:
    def test_passes_when_no_t3_on_path(self, tmp_path: Path) -> None:
        ok, message = _run(str(tmp_path / "bin"), tmp_path)
        assert ok is True
        assert "WARN" not in message
        assert "FAIL" not in message

    def test_passes_when_only_uv_bin_has_t3(self, tmp_path: Path) -> None:
        uv_bin = tmp_path / ".local" / "bin"
        uv_bin.mkdir(parents=True)
        (uv_bin / "t3").touch()

        ok, message = _run(str(uv_bin), tmp_path)
        assert ok is True
        assert "WARN" not in message

    def test_fails_when_shadowing_entry_precedes_uv_bin(self, tmp_path: Path) -> None:
        uv_bin = tmp_path / ".local" / "bin"
        uv_bin.mkdir(parents=True)
        (uv_bin / "t3").touch()

        shim_dir = tmp_path / ".pyenv" / "shims"
        shim_dir.mkdir(parents=True)
        (shim_dir / "t3").touch()

        env_path = f"{shim_dir}:{uv_bin}"
        ok, message = _run(env_path, tmp_path)

        assert ok is False
        assert "FAIL" in message
        assert str(shim_dir / "t3") in message
        assert str(uv_bin / "t3") in message

    def test_passes_when_uv_bin_precedes_other_entries(self, tmp_path: Path) -> None:
        uv_bin = tmp_path / ".local" / "bin"
        uv_bin.mkdir(parents=True)
        (uv_bin / "t3").touch()

        other_dir = tmp_path / "other" / "bin"
        other_dir.mkdir(parents=True)
        (other_dir / "t3").touch()

        env_path = f"{uv_bin}:{other_dir}"
        ok, message = _run(env_path, tmp_path)

        assert ok is True
        assert "FAIL" not in message

    def test_respects_uv_tool_bin_dir_env(self, tmp_path: Path) -> None:
        custom_uv_bin = tmp_path / "custom" / "uv" / "bin"
        custom_uv_bin.mkdir(parents=True)
        (custom_uv_bin / "t3").touch()

        shim_dir = tmp_path / ".pyenv" / "shims"
        shim_dir.mkdir(parents=True)
        (shim_dir / "t3").touch()

        env_path = f"{shim_dir}:{custom_uv_bin}"
        ok, message = _run(env_path, tmp_path, uv_tool_bin_dir=str(custom_uv_bin))

        assert ok is False
        assert "FAIL" in message
        assert str(shim_dir / "t3") in message

    def test_message_includes_fix_instructions(self, tmp_path: Path) -> None:
        uv_bin = tmp_path / ".local" / "bin"
        uv_bin.mkdir(parents=True)
        (uv_bin / "t3").touch()

        shim_dir = tmp_path / ".pyenv" / "shims"
        shim_dir.mkdir(parents=True)
        (shim_dir / "t3").touch()

        env_path = f"{shim_dir}:{uv_bin}"
        ok, message = _run(env_path, tmp_path)

        assert ok is False
        assert "rm" in message or "remove" in message.lower()

    def test_multiple_shadows_all_reported(self, tmp_path: Path) -> None:
        uv_bin = tmp_path / ".local" / "bin"
        uv_bin.mkdir(parents=True)
        (uv_bin / "t3").touch()

        shadow1 = tmp_path / ".pyenv" / "shims"
        shadow1.mkdir(parents=True)
        (shadow1 / "t3").touch()

        shadow2 = tmp_path / "usr" / "local" / "bin"
        shadow2.mkdir(parents=True)
        (shadow2 / "t3").touch()

        env_path = f"{shadow1}:{shadow2}:{uv_bin}"
        ok, message = _run(env_path, tmp_path)

        assert ok is False
        assert str(shadow1 / "t3") in message
        assert str(shadow2 / "t3") in message
