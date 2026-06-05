"""The OVER-DENY gates honour the master fail-open switch + self-rescue.

Each gate that can wedge the factory on a detection misfire routes its
deny through ``_fail_open_or_deny``. This file asserts the SHARED escape
applies to each gate end-to-end: with ``[teatree] gate_fail_open = true``
recorded, the gate that would normally deny instead passes through.

The PUBLIC-egress leak gate is deliberately NOT covered here — it stays
fail-closed (see ``test_public_leak_gate_stays_fail_closed.py``).
"""

import json
from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import handle_enforce_skill_loading, handle_validate_mr_metadata


def _capture(handler, data: dict) -> tuple[bool, dict | None]:
    buf = StringIO()
    with patch("sys.stdout", buf):
        blocked = handler(data)
    raw = buf.getvalue().strip()
    return blocked, (json.loads(raw) if raw else None)


def _write_fail_open(home: Path, *, on: bool) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / ".teatree.toml").write_text(
        f"[teatree]\ngate_fail_open = {'true' if on else 'false'}\n",
        encoding="utf-8",
    )


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


# ── validate-mr broken-env ──────────────────────────────────────────


def _mr_create() -> dict:
    return {
        "tool_name": "Bash",
        "tool_input": {"command": "glab mr create --title 'bad' --description 'x'"},
    }


class TestValidateMrBrokenEnvRouting:
    def test_denies_on_broken_env_when_fail_open_off(self, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_fail_open(home, on=False)
        # Force the "validator unresolvable" broken-env branch.
        monkeypatch.setattr(router, "_mr_validate_argv", lambda: None)
        monkeypatch.delenv("T3_MR_VALIDATE_ALLOW_BROKEN_ENV", raising=False)
        blocked, payload = _capture(handle_validate_mr_metadata, _mr_create())
        assert blocked is True
        assert payload is not None
        assert payload["permissionDecision"] == "deny"

    def test_passes_through_on_broken_env_when_fail_open_on(self, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_fail_open(home, on=True)
        monkeypatch.setattr(router, "_mr_validate_argv", lambda: None)
        monkeypatch.delenv("T3_MR_VALIDATE_ALLOW_BROKEN_ENV", raising=False)
        blocked, payload = _capture(handle_validate_mr_metadata, _mr_create())
        assert blocked is False
        assert payload is None


# ── skill-loading ───────────────────────────────────────────────────


@pytest.fixture
def skills_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    original_state = router.STATE_DIR
    router.STATE_DIR = tmp_path / "state"
    router.STATE_DIR.mkdir(parents=True, exist_ok=True)
    skills = tmp_path / "skills"
    (skills / "code").mkdir(parents=True, exist_ok=True)
    (skills / "code" / "SKILL.md").write_text("---\nname: code\n---\n", encoding="utf-8")
    monkeypatch.setenv("T3_SKILL_SEARCH_DIRS", str(skills))
    yield skills
    router.STATE_DIR = original_state


def _seed_pending(skills: list[str]) -> None:
    (router.STATE_DIR / "s1.pending").write_text("\n".join(skills) + "\n", encoding="utf-8")


class TestSkillLoadingGateRouting:
    def _edit(self) -> dict:
        return {
            "session_id": "s1",
            "tool_name": "Edit",
            "tool_input": {"file_path": "/x.py", "old_string": "a", "new_string": "b"},
        }

    def test_denies_with_unloaded_resolvable_skill_when_fail_open_off(self, home: Path, skills_dir: Path) -> None:
        _write_fail_open(home, on=False)
        _seed_pending(["code"])
        blocked, payload = _capture(handle_enforce_skill_loading, self._edit())
        assert blocked is True
        assert payload is not None
        assert payload["permissionDecision"] == "deny"

    def test_passes_through_when_fail_open_on(self, home: Path, skills_dir: Path) -> None:
        _write_fail_open(home, on=True)
        _seed_pending(["code"])
        blocked, payload = _capture(handle_enforce_skill_loading, self._edit())
        assert blocked is False
        assert payload is None
