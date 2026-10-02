"""The disk-pressure guidance warns that a borrowed venv is a destroyed venv.

Aiming ``UV_PROJECT_ENVIRONMENT`` at another checkout's ``.venv`` to save disk gets that
venv re-synced — and deleted and rebuilt outright — by the borrower's ``uv run``. The
warning belongs where an operator short of disk is reading, beside the real disk lever.
"""
# test-path: cross-cutting — a prose invariant over a skill file; no teatree module is under test.

import re
from pathlib import Path

_SKILL = Path(__file__).resolve().parents[1] / "skills" / "workspace" / "SKILL.md"
_SECTION = re.compile(r"^### The checkout pool's retention policy.*?(?=^### )", re.MULTILINE | re.DOTALL)
_RADIUS = 400


def _windows(text: str, token: str) -> list[str]:
    return [text[max(0, m.start() - _RADIUS) : m.end() + _RADIUS] for m in re.finditer(re.escape(token), text)]


def test_the_retention_policy_warns_a_borrowed_venv_is_rebuilt_and_names_the_disk_lever() -> None:
    section = _SECTION.search(_SKILL.read_text(encoding="utf-8"))
    assert section, "the retention policy section is gone from skills/workspace/SKILL.md"
    assert any(
        "rebuilds" in window and "retention artifacts" in window
        for window in _windows(section.group(0), "UV_PROJECT_ENVIRONMENT")
    )
