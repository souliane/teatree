"""Tests for the PreToolUse skill-loading gate's fail-open on stale skills.

The skill-loading gate (``handle_enforce_skill_loading``) blocks
Bash/Edit/Write until every suggested-but-unloaded skill is loaded. A
suggestion comes from the supplementary keyword config
(``~/.teatree-skills.yml``) or from lifecycle/intent detection, and lands
in ``<session>.pending``.

The lockout class this guards against: a ``~/.teatree-skills.yml`` entry
maps a keyword to a skill *name that no longer resolves* (renamed or
removed skill). The gate would then demand a skill the ``Skill`` tool
cannot load ("Unknown skill"), blocking ALL Bash/Edit/Write for the whole
session with no in-session self-rescue.

The fix: before blocking on a required skill, the gate verifies the name
resolves to a loadable skill (a ``<skill>/SKILL.md`` under one of the
skill search dirs). An unresolvable name does NOT block — the gate emits
a one-line warning naming the stale skill + the config file and lets the
tool through. A real-but-unloaded skill still enforces load-first.

Integration-style: the real handler, real ``STATE_DIR`` on ``tmp_path``,
real skill dirs seeded under the temp ``HOME``.
"""

import json
from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import handle_enforce_skill_loading


@pytest.fixture
def gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point ``STATE_DIR`` at a temp dir and seed a ``~/.claude/skills`` tree.

    ``HOME`` is already temp-isolated by ``conftest._isolate_env``. This
    fixture creates the skills directory the resolver scans and seeds one
    real, loadable skill (``ac-reviewing-codebase``) so tests can
    distinguish "real but unloaded" from "stale / unresolvable".

    Returns the skills dir so tests can add more skills if needed.
    """
    original_state = router.STATE_DIR
    router.STATE_DIR = tmp_path / "state"
    router.STATE_DIR.mkdir(parents=True, exist_ok=True)

    skills_dir = Path.home() / ".claude" / "skills"
    real_skill = skills_dir / "ac-reviewing-codebase"
    real_skill.mkdir(parents=True, exist_ok=True)
    (real_skill / "SKILL.md").write_text("---\nname: ac-reviewing-codebase\n---\n", encoding="utf-8")

    yield skills_dir

    router.STATE_DIR = original_state


def _write_pending(session_id: str, skills: list[str]) -> None:
    (router.STATE_DIR / f"{session_id}.pending").write_text("\n".join(skills) + "\n", encoding="utf-8")


def _write_loaded(session_id: str, skills: list[str]) -> None:
    (router.STATE_DIR / f"{session_id}.skills").write_text("\n".join(skills) + "\n", encoding="utf-8")


def _run(data: dict) -> tuple[bool, dict | None, str]:
    """Invoke the gate, capturing its deny payload (stdout) and warning (stderr)."""
    out = StringIO()
    err = StringIO()
    with patch("sys.stdout", out), patch("sys.stderr", err):
        blocked = handle_enforce_skill_loading(data)
    payload = None
    raw = out.getvalue().strip()
    if raw:
        payload = json.loads(raw)
    return blocked, payload, err.getvalue()


class TestStaleSkillFailsOpen:
    """A pending skill whose name does not resolve must NOT block tools."""

    @pytest.mark.parametrize("tool_name", ["Bash", "Edit", "Write"])
    def test_unresolvable_skill_does_not_block(self, gate: Path, tool_name: str) -> None:
        _write_pending("sess-stale", ["ac-auditing-repos"])
        blocked, payload, _ = _run({"session_id": "sess-stale", "tool_name": tool_name})
        assert blocked is False
        assert payload is None

    def test_unresolvable_skill_warns_with_name_and_config(self, gate: Path) -> None:
        _write_pending("sess-stale2", ["ac-auditing-repos"])
        _, _, warning = _run({"session_id": "sess-stale2", "tool_name": "Bash"})
        assert "ac-auditing-repos" in warning
        assert ".teatree-skills.yml" in warning


class TestRealUnloadedSkillStillEnforced:
    """A pending skill that DOES resolve but is unloaded still blocks (load-first)."""

    def test_real_unloaded_skill_blocks(self, gate: Path) -> None:
        _write_pending("sess-real", ["ac-reviewing-codebase"])
        blocked, payload, _ = _run({"session_id": "sess-real", "tool_name": "Bash"})
        assert blocked is True
        assert payload is not None
        assert payload["permissionDecision"] == "deny"
        assert "ac-reviewing-codebase" in payload["permissionDecisionReason"]

    def test_real_loaded_skill_passes(self, gate: Path) -> None:
        _write_pending("sess-loaded", ["ac-reviewing-codebase"])
        _write_loaded("sess-loaded", ["ac-reviewing-codebase"])
        blocked, payload, _ = _run({"session_id": "sess-loaded", "tool_name": "Bash"})
        assert blocked is False
        assert payload is None


class TestMixedResolvability:
    """A mix of a stale name and a real-unloaded name blocks only on the real one."""

    def test_blocks_on_real_warns_on_stale(self, gate: Path) -> None:
        _write_pending("sess-mixed", ["ac-auditing-repos", "ac-reviewing-codebase"])
        blocked, payload, warning = _run({"session_id": "sess-mixed", "tool_name": "Bash"})
        assert blocked is True
        assert payload is not None
        assert "ac-reviewing-codebase" in payload["permissionDecisionReason"]
        # The stale name must not appear as a load-me demand.
        assert "ac-auditing-repos" not in payload["permissionDecisionReason"]
        assert "ac-auditing-repos" in warning
