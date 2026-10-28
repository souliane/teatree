from pathlib import Path

import pytest

from teatree.agents import skill_injection
from teatree.skill_support import loading
from teatree.skill_support.index import build_skill_index
from teatree.skill_support.loading import _PHASE_TO_SKILL, SkillLoadingPolicy
from tests.quality._anchor_prose import covered_bytes, load_ledger, window_digests

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills/interactive/SKILL.md"


def _answer_policy() -> str:
    section = SKILL.read_text(encoding="utf-8").split("## Resolve settled owner cards and agent asks", 1)[1]
    return section.split("\n## ", 1)[0]


def test_attended_answer_scope_names_headless_mailbox_and_owner_limit() -> None:
    assert "A headless orchestrator may answer its own agents' scope and architecture questions" in _answer_policy()
    assert "it does not dismiss live owner cards" in _answer_policy()


def test_layer_rule_is_once_in_rules_and_standing_answers_cover_recurring_cases() -> None:
    stub = (ROOT / "skills/rules/SKILL.md").read_text(encoding="utf-8")
    reference = (ROOT / "skills/rules/references/asking-questions.md").read_text(encoding="utf-8")
    assert stub.count("Decide from the standing answers and verified facts first") == 1
    assert "headless agent asks its orchestrator through the direct mailbox" in stub
    for case in (
        "Working folder",
        "Held pull request",
        "Round ceiling",
        "Repair halt",
        "Stuck ticket",
        "Unrecordable review",
        "News batch",
        "Agent-asked implementation details",
    ):
        assert reference.count(f"**{case}:**") == 1


def test_attended_answer_uses_interactive_resolver() -> None:
    assert "questions dismiss <id> --resolver agent:interactive" in _answer_policy()


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
    ["This hold requires the owner's per-PR authorization", "A headless orchestrator may answer"],
)
def test_blueprint_owner_rule_clauses_are_pegged(anchor: str) -> None:
    _assert_prose_peg(anchor, "BLUEPRINT.md")


def test_checkout_policy_names_factory_retry_without_an_owner_card() -> None:
    workspace = (ROOT / "skills/workspace/SKILL.md").read_text(encoding="utf-8")
    interactive = SKILL.read_text(encoding="utf-8")
    assert "Unsafe checkout state stays parked for an internal retry" in workspace
    assert "For a checkout card," not in interactive
    assert "A refusal that cannot be attested asks the owner in Slack" not in workspace


def test_factory_checkout_route_is_pegged() -> None:
    _assert_prose_peg("Unsafe checkout state stays parked for an internal retry", "skills/workspace/SKILL.md")


def test_attended_checkout_teaches_custody_proven_factory_recovery() -> None:
    windows = _assert_prose_peg("Factory retries may re-attest interrupted writer files", "skills/interactive/SKILL.md")
    assert any("this does not attest hand edits" in window for window in windows)


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
    assert "Resolve settled owner cards and agent asks" in text
    assert "below Rules 0-5" in text
    assert limit in text


@pytest.mark.parametrize("command", ["questions list", "questions dismiss"])
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


def test_every_runtime_phase_loads_the_shared_layer_rule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(loading, "harness_skills_dirs", lambda: [ROOT / "skills"])
    policy = SkillLoadingPolicy()
    index = build_skill_index([ROOT / "skills"])
    for phase in _PHASE_TO_SKILL:
        bundle = policy.select_for_runtime_phase(
            cwd=tmp_path, phase=phase, overlay_skill_metadata={}, skill_index=index
        )
        assert "rules" in bundle.skills, phase
