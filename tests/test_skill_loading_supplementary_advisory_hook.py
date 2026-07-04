r"""Supplementary keyword skills must not hard-block tool calls (#1683).

The supplementary keyword config (``~/.teatree-skills.yml``) maps loose
regexes to skill names — e.g. ``ac-adopting-ruff: '\b(ruff|...)\b'``. The
bare ``\bruff\b`` alternative matches ANY mention of the word ``ruff`` in a
genuine prompt ("can you run ruff check on the changed files"), not just the
adoption intent. Pre-fix, that match landed in ``<session>.pending`` and the
PreToolUse gate (``handle_enforce_skill_loading``) then hard-blocked every
Bash/Edit/Write until ``/ac-adopting-ruff`` loaded — constant friction that
trained reflexive ``[skill-load-ok:]`` bypasses (#1683).

This is distinct from the #1567 ambient-context strip: here the keyword is in
the GENUINE prompt body, not a harness ``<system-reminder>`` block.

The fix demotes supplementary-config skills from the hard-block demand set to
advisory-only: they are still SUGGESTED (the "LOAD THESE SKILLS" message), but
are NOT written to ``<session>.pending``, so the gate never hard-blocks an
incidental keyword match. Intent / framework / overlay / companion skills —
which carry priority/exclude/word-boundary discipline in the trigger index —
keep enforcing load-first. A skill that is ALSO an intent/framework skill
stays in the demand set; only supplementary-ONLY skills are demoted.

Integration-style: the real ``suggest_skills`` engine, a real trigger index
built from fixture ``SKILL.md`` files, a real ``~/.teatree-skills.yml``-shaped
config on disk, and the real ``handle_user_prompt_submit`` pending writer.
"""

from __future__ import annotations  # noqa: TID251

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import handle_user_prompt_submit

if TYPE_CHECKING:
    from collections.abc import Iterator

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib import skill_loader as skill_loader_mod  # noqa: E402
from lib.skill_loader import suggest_skills  # noqa: E402

_RUFF_CONFIG = "ac-adopting-ruff: '\\b(ruff|adopt.*ruff|migrate.*ruff)\\b'\n"


@pytest.fixture
def fixtures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    r"""Seed a fixture skills tree, a ruff supplementary config, and an empty cache.

    ``code`` is a real lifecycle intent skill (trigger ``\bfix\b``) so a
    genuine task intent fires the supplementary layer. ``ac-adopting-ruff`` is
    a supplementary-only skill with no trigger index entry. The XDG metadata
    cache is pointed at a missing path so the trigger index builds from the
    fixture dir (deterministic, host-independent).
    """
    skills_dir = tmp_path / "skills"
    (skills_dir / "ac-adopting-ruff").mkdir(parents=True, exist_ok=True)
    (skills_dir / "ac-adopting-ruff" / "SKILL.md").write_text("---\nname: ac-adopting-ruff\n---\n", encoding="utf-8")
    (skills_dir / "code").mkdir(parents=True, exist_ok=True)
    (skills_dir / "code" / "SKILL.md").write_text(
        "---\nname: code\ntriggers:\n  priority: 70\n  keywords:\n    - '\\bfix\\b'\n---\n",
        encoding="utf-8",
    )

    config = tmp_path / ".teatree-skills.yml"
    config.write_text(_RUFF_CONFIG, encoding="utf-8")

    monkeypatch.setattr(skill_loader_mod, "SKILL_METADATA_CACHE", tmp_path / "no-cache.json")
    monkeypatch.setattr(
        skill_loader_mod, "read_overlay_skill_metadata", lambda: {"skill_path": "", "remote_patterns": []}
    )
    monkeypatch.setattr(skill_loader_mod, "read_overlay_companion_skills", list)

    return skills_dir, config


def _run(prompt: str, skills_dir: Path, config: Path) -> dict:
    return suggest_skills(
        {
            "prompt": prompt,
            "cwd": str(skills_dir.parent),
            "loaded_skills": [],
            "skill_search_dirs": [str(skills_dir)],
            "supplementary_config": str(config),
        }
    )


class TestSupplementaryDemotedToAdvisory:
    """A supplementary keyword match is suggested but never a hard demand."""

    def test_incidental_ruff_mention_is_advisory_not_demanded(self, fixtures: tuple[Path, Path]) -> None:
        # "fix" drives the real ``code`` intent; the in-body ``ruff`` word
        # incidentally matches the supplementary mapping. The ruff skill must
        # be suggested but kept OUT of the hard-block (advisory) set.
        skills_dir, config = fixtures
        result = _run("fix the failing test and run ruff check", skills_dir, config)
        assert "ac-adopting-ruff" in result["suggestions"]
        assert "ac-adopting-ruff" in result["advisory"]

    def test_framework_skill_is_not_advisory(self, fixtures: tuple[Path, Path]) -> None:
        # A cwd-detected framework skill (``ac-django``) is a hard demand, never
        # advisory — only supplementary-config skills are demoted.
        skills_dir, config = fixtures
        (skills_dir.parent / "manage.py").touch()
        result = _run("fix the failing test and run ruff check", skills_dir, config)
        assert "ac-django" in result["suggestions"]
        assert "ac-django" not in result["advisory"]


class TestPendingExcludesAdvisory:
    """The hook writes only the hard-demand subset to ``<session>.pending``.

    Patches the loader so the assertion is on the hook's pending-writer split
    (the actual hook change), independent of host config / search dirs.
    """

    @pytest.fixture
    def state_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
        original = router.STATE_DIR
        router.STATE_DIR = tmp_path / "state"
        router.STATE_DIR.mkdir(parents=True, exist_ok=True)
        # #256: the suggester is gated on teatree engagement; opt in via autoload
        # so this test exercises the pending-writer split, not the default-off gate.
        monkeypatch.setenv("T3_AUTOLOAD", "1")
        monkeypatch.setattr(
            skill_loader_mod,
            "suggest_skills",
            lambda _input: {
                "suggestions": ["code", "ac-django", "ac-adopting-ruff"],
                "advisory": ["ac-adopting-ruff"],
                "intent": "code",
            },
        )
        yield router.STATE_DIR
        router.STATE_DIR = original

    def _pending(self, session_id: str) -> list[str]:
        path = router.STATE_DIR / f"{session_id}.pending"
        if not path.is_file():
            return []
        return [line for line in path.read_text(encoding="utf-8").splitlines() if line]

    def _submit(self, session_id: str) -> None:
        handle_user_prompt_submit({"session_id": session_id, "prompt": "fix the bug and run ruff check"})

    def test_supplementary_skill_not_in_pending_hard_block(
        self, state_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._submit("sess-ruff")
        message = capsys.readouterr().out
        # Demoted out of the hard-block demand set ...
        assert "ac-adopting-ruff" not in self._pending("sess-ruff")
        # ... but still surfaced in the LOAD-THESE-SKILLS suggestion message.
        assert "ac-adopting-ruff" in message

    def test_intent_and_framework_skills_still_in_pending_hard_block(
        self, state_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The pending writer normalizes owned skills UP to their namespace
        # (``code`` -> ``t3:code``); supplementary ``ac-*`` names stay bare.
        self._submit("sess-code")
        capsys.readouterr()
        pending = self._pending("sess-code")
        assert "t3:code" in pending
        assert "ac-django" in pending
