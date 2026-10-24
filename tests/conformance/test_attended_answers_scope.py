from pathlib import Path

import pytest

from teatree.agents import skill_injection
from teatree.skill_support.loading import _PHASE_TO_SKILL, SkillLoadingPolicy
from tests.quality._anchor_prose import covered_bytes, load_ledger, window_digests

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills/interactive/SKILL.md"


def _answer_policy() -> str:
    return (
        SKILL.read_text(encoding="utf-8").split("## Answer the owner's cards in his place", 1)[1].split("\n## ", 1)[0]
    )


def test_attended_answer_scope_excludes_headless_runs() -> None:
    assert (
        "This applies only to this attended session, below Rules 0-5. "
        "A headless factory run never answers a card addressed to the owner in his place."
    ) in _answer_policy()


def test_checkout_proof_requires_no_process_inside() -> None:
    assert "and has no process inside it" in _answer_policy()


def test_attended_answer_uses_interactive_resolver() -> None:
    assert "questions dismiss <id> --resolver agent:interactive" in _answer_policy()


@pytest.mark.parametrize(
    "fact",
    [
        "standing instruction of 2026-10-22",
        "read-only re-check",
        "expected head",
        "clean tree",
        "no merge in progress",
        "working directory, an open file, a mapped file, or a bound socket",
        "host process table is unusable",
        "pseudo-terminal",
        "clean environment",
        "audit trail reads as a direct owner command",
        "plainly report that it ran the command",
        "leave it on the owner's card",
    ],
)
def test_checkout_card_attestation_route_names_each_condition(fact: str) -> None:
    assert fact in _answer_policy()


def _assert_prose_peg(anchor: str, path: str, *, expected_paths: set[str] | None = None) -> list[str]:
    ledger = load_ledger(anchor)
    assert set(ledger.pegs) == (expected_paths or {path})
    prose = (ROOT / path).read_text(encoding="utf-8")
    actual = window_digests(prose, anchor, ledger.radius)
    assert actual
    assert [sha for sha, _window in actual] == list(ledger.pegs[path])
    assert covered_bytes(prose, anchor, ledger.radius) == ledger.coverage[path]
    return [window for _sha, window in actual]


@pytest.mark.parametrize(
    "anchor",
    ["This hold requires the owner's per-PR authorization", "Only an attended session may answer"],
)
def test_blueprint_owner_rule_clauses_are_pegged(anchor: str) -> None:
    _assert_prose_peg(anchor, "BLUEPRINT.md")


def test_checkout_card_paragraph_is_pegged() -> None:
    windows = _assert_prose_peg("For a checkout card,", "skills/interactive/SKILL.md")
    paragraph = (
        "When the facts settle a card"
        + SKILL.read_text(encoding="utf-8").split("\n\nWhen the facts settle a card", 1)[1].split("\n\n", 1)[0]
    )
    assert any(paragraph in window for window in windows)


@pytest.mark.parametrize(
    ("anchor", "path"),
    [
        ("An attended session may attest under", "skills/workspace/SKILL.md"),
        ("An attended session may attest under", "docs/blueprint/worktree-occupancy.md"),
    ],
    ids=["workspace", "worktree-occupancy"],
)
def test_checkout_route_reconciliation_is_pegged(anchor: str, path: str) -> None:
    _assert_prose_peg(
        anchor,
        path,
        expected_paths={"skills/workspace/SKILL.md", "docs/blueprint/worktree-occupancy.md"},
    )


@pytest.mark.parametrize(
    "limit",
    [
        "spending money",
        "irreversible deletion of work that exists on no remote",
        "widening a security posture or a credential's use",
        "anything said to a colleague in the owner's name",
    ],
)
def test_attended_answer_policy_keeps_owner_limits(limit: str) -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert "Answer the owner's cards in his place (attended session only)" in text
    assert "below Rules 0-5" in text
    assert limit in text


@pytest.mark.parametrize("command", ["questions list", "questions dismiss", "worktree attest"])
def test_attended_answer_commands_exist(command: str) -> None:
    text = SKILL.read_text(encoding="utf-8")
    reference = (ROOT / "docs/generated/cli-reference.md").read_text(encoding="utf-8")
    assert f"t3 <overlay> {command}" in text
    assert f"t3 teatree {command}`" in reference


def test_headless_rules_do_not_answer_owner_cards(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(skill_injection, "harness_skills_dirs", lambda: [ROOT / "skills"])
    rendered = skill_injection.read_only_venue_skill_contents(["rules"], {"rules"})
    assert "0. **Nothing may endanger security or leak:**" in rendered
    for retired in ("Rule " + "-1", "Impersonate", "in the owner" + "'s place", "in his place"):
        assert retired not in rendered


def test_runtime_phases_do_not_load_the_attended_skill(tmp_path: Path) -> None:
    policy = SkillLoadingPolicy()
    for phase in _PHASE_TO_SKILL:
        bundle = policy.select_for_runtime_phase(cwd=tmp_path, phase=phase, overlay_skill_metadata={})
        assert bundle.lifecycle_skill
        assert bundle.skills
        assert "interactive" not in bundle.skills
