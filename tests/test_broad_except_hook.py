"""Tests for the broad-except-must-observe pre-commit hook (#1987).

A broad-except handler (``except Exception`` / ``except BaseException`` / bare
``except:``) that swallows — no log, no re-raise — is a fail-open seam. The
hook flags it. It also flags a broad handler returning a gate success sentinel
(``return True``), which is the schema_guard fail-open class. An auditable
opt-out registry exempts declared tick-safety-net / resolve-or-skip seams.

Staged warn-first (manual stage) per the warn-not-fail-on-imperfect-heuristic
binding — the existing 116 legit broad handlers must not block commits before
the class is fully migrated.
"""

from pathlib import Path

import pytest

import scripts.hooks.check_broad_except as mod

_SWALLOW = """\
def f():
    try:
        risky()
    except Exception:
        return None
"""

_LOGS = """\
import logging

logger = logging.getLogger(__name__)


def f():
    try:
        risky()
    except Exception:
        logger.warning("risky failed")
        return None
"""

_RERAISES = """\
def f():
    try:
        risky()
    except Exception:
        raise
"""

_WRAPS_AND_RAISES = """\
def f():
    try:
        risky()
    except Exception as exc:
        raise RuntimeError("wrapped") from exc
"""

_RETURNS_SUCCESS_SENTINEL = """\
def check() -> bool:
    try:
        risky()
    except Exception:
        return True
    return False
"""

_NARROW_EXCEPT = """\
def f():
    try:
        risky()
    except ValueError:
        return None
"""

_ECHOES_AND_FAILS_CLOSED = """\
import typer


def check() -> bool:
    try:
        risky()
    except Exception:
        typer.echo("FAIL  risky errored")
        return False
    return True
"""

_BARE_EXCEPT_SWALLOW = """\
def f():
    try:
        risky()
    except:
        return None
"""


def _write(tmp_path: Path, rel: str, source: str) -> str:
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    return rel


def _run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, staged: list[str], optout: str = "optouts: []\n") -> int:
    optout_path = tmp_path / "optout.yaml"
    optout_path.write_text(optout, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mod, "_staged_python_files", lambda: staged)
    monkeypatch.setattr(mod, "_OPTOUT_REGISTRY", optout_path)
    return mod.main()


class TestFlagsSwallows:
    def test_broad_except_returning_without_log_or_raise_is_flagged(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        rel = _write(tmp_path, "src/teatree/x.py", _SWALLOW)
        assert _run(monkeypatch, tmp_path, [rel]) == 1

    def test_bare_except_swallow_is_flagged(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        rel = _write(tmp_path, "src/teatree/x.py", _BARE_EXCEPT_SWALLOW)
        assert _run(monkeypatch, tmp_path, [rel]) == 1

    def test_broad_except_returning_success_sentinel_is_flagged(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        rel = _write(tmp_path, "src/teatree/x.py", _RETURNS_SUCCESS_SENTINEL)
        assert _run(monkeypatch, tmp_path, [rel]) == 1


class TestPassesObservableHandlers:
    def test_handler_that_logs_passes(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        rel = _write(tmp_path, "src/teatree/x.py", _LOGS)
        assert _run(monkeypatch, tmp_path, [rel]) == 0

    def test_handler_that_reraises_passes(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        rel = _write(tmp_path, "src/teatree/x.py", _RERAISES)
        assert _run(monkeypatch, tmp_path, [rel]) == 0

    def test_handler_that_wraps_and_raises_passes(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        rel = _write(tmp_path, "src/teatree/x.py", _WRAPS_AND_RAISES)
        assert _run(monkeypatch, tmp_path, [rel]) == 0

    def test_narrow_except_passes(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        rel = _write(tmp_path, "src/teatree/x.py", _NARROW_EXCEPT)
        assert _run(monkeypatch, tmp_path, [rel]) == 0

    def test_handler_that_echoes_and_fails_closed_passes(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        rel = _write(tmp_path, "src/teatree/x.py", _ECHOES_AND_FAILS_CLOSED)
        assert _run(monkeypatch, tmp_path, [rel]) == 0


class TestOptOutRegistry:
    def test_opted_out_file_is_exempt(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        rel = _write(tmp_path, "src/teatree/safety_net.py", _SWALLOW)
        optout = "optouts:\n  - file: src/teatree/safety_net.py\n    reason: tick safety net\n"
        assert _run(monkeypatch, tmp_path, [rel], optout=optout) == 0


class TestRegistryIsWellFormed:
    def test_real_registry_parses_and_lists_existing_files(self) -> None:
        entries = mod.load_optouts(mod._OPTOUT_REGISTRY)
        repo_root = Path(__file__).resolve().parent.parent
        for entry in entries:
            assert (repo_root / entry).exists(), f"opt-out names a missing file: {entry}"
