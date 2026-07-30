"""Seed the active-skills set at engaged SessionStart (#3273).

On an autoloaded session ``handle_session_start_bootstrap`` engages the session
but never wrote ``<session>.skills`` — that file is only written on an explicit
Skill/InstructionsLoaded event, so a fresh autoloaded session showed NO skills
until the user manually invoked a ``/t3:`` skill. The fix seeds the lifecycle
core skill set at engagement, keeping the "loaded this session" semantics for
non-autoload sessions unchanged.
"""

from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.engagement import LIFECYCLE_SEED_SKILLS, engage


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(router, "STATE_DIR", state)
    return state


def _skills(state: Path, session_id: str) -> list[str]:
    path = state / f"{session_id}.skills"
    if not path.is_file():
        return []
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


class TestEngageSeedsSkills:
    def test_engage_with_seed_writes_the_lifecycle_set(self, state_dir: Path) -> None:
        engage("s-seed", seed_skills=True)

        seeded = _skills(state_dir, "s-seed")
        assert seeded, "an engaged session must seed a non-empty skills set"
        # The seed is the lifecycle core — the smaller meaningful set the owner
        # expects to see, not the full available catalogue.
        assert set(seeded) == set(LIFECYCLE_SEED_SKILLS)

    def test_engage_without_seed_writes_no_skills(self, state_dir: Path) -> None:
        engage("s-plain")

        assert _skills(state_dir, "s-plain") == []
        # Engagement itself still records the active marker.
        assert (state_dir / "s-plain.teatree-active").is_file()

    def test_seed_preserves_and_augments_existing_skills(self, state_dir: Path) -> None:
        skills_file = state_dir / "s-aug.skills"
        skills_file.write_text("t3:code\nsome-overlay:playbook\n", encoding="utf-8")

        engage("s-aug", seed_skills=True)

        seeded = _skills(state_dir, "s-aug")
        # A pre-existing loaded skill is preserved, never clobbered.
        assert "some-overlay:playbook" in seeded
        # No duplicate for a skill already present in the file.
        assert seeded.count("t3:code") == 1
        # The rest of the lifecycle set is appended.
        assert set(LIFECYCLE_SEED_SKILLS) <= set(seeded)

    def test_empty_session_id_is_a_noop(self, state_dir: Path) -> None:
        engage("", seed_skills=True)
        assert not list(state_dir.glob("*.skills"))
