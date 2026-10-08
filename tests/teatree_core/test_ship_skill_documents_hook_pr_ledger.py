# test-path: cross-cutting — asserts a skills/ship doc invariant; the
# external_delivery import is only the lease TTL constant, not the unit under test.
"""A PR the no-orphan hook opens must land on its ticket's ``PullRequest`` ledger.

The hook resolves the owning ticket only through a ``Worktree`` row on the branch,
so a checkout made by hand opens PR #4903 with no ledger row: shipping read "no
shippable diff" and ignored the ticket, and the rubric gate never bound. The rule
lives in ship/SKILL.md § 4a1; code and test carry the pointer because the coder and
tester do the first push and never load ship. Per ``/t3:code`` § 5d each nearness
assertion scans every occurrence of its anchor.
"""

from pathlib import Path

from teatree.core.models.external_delivery import LEASE_SECONDS

_SKILLS = Path(__file__).resolve().parents[2] / "skills"
_SHIP_SKILL = _SKILLS / "ship" / "SKILL.md"
_REFERENCE = _SKILLS / "ship" / "references" / "hook-opened-pr-ledger.md"
_POINTER_SKILLS = (_SKILLS / "code" / "SKILL.md", _SKILLS / "test" / "SKILL.md")

_ADOPT_COMMAND = "t3 <overlay> workspace ticket <issue-url> --adopt"
_LEDGER_READ = "mcp__teatree__pr_for_ticket"


def _any_window_contains(text: str, anchor: str, *, must_include: str, radius: int) -> bool:
    start = 0
    while (idx := text.find(anchor, start)) != -1:
        if must_include in text[max(0, idx - radius) : idx + len(anchor) + radius]:
            return True
        start = idx + 1
    return False


def _section_4a1(text: str) -> str:
    start = text.find("\n### 4a1.")
    assert start != -1, "ship/SKILL.md has no `### 4a1.` section"
    end = text.find("\n### ", start + 1)
    return text[start:end]


class TestShipSpineOwnsTheRule:
    def test_4a1_is_non_negotiable_and_sits_between_4a_and_4b(self) -> None:
        text = _SHIP_SKILL.read_text(encoding="utf-8")
        heading = _section_4a1(text).strip().splitlines()[0]
        assert "(Non-Negotiable)" in heading
        assert text.index("\n### 4a. ") < text.index("\n### 4a1.") < text.index("\n### 4b. ")

    def test_4a1_names_the_register_command_the_ledger_read_and_the_reference(self) -> None:
        section = _section_4a1(_SHIP_SKILL.read_text(encoding="utf-8"))
        for token in (_ADOPT_COMMAND, _LEDGER_READ, "skills/ship/references/hook-opened-pr-ledger.md"):
            assert token in section, f"ship/SKILL.md § 4a1 must name `{token}`"


class TestReferenceCarriesTheMechanism:
    def test_reference_cites_the_lookup_the_gate_and_the_non_healing_skip(self) -> None:
        text = _REFERENCE.read_text(encoding="utf-8")
        for symbol in (
            "teatree.core.management.commands._ensure_pr._ticket_for_branch",
            "teatree.core.merge.ticket_resolution.resolve_gated_ticket",
            "teatree.core.management.commands._ensure_pr.skip_for_classified",
        ):
            assert symbol in text, f"hook-opened-pr-ledger.md must cite `{symbol}`"

    def test_reference_says_rerunning_ensure_pr_records_nothing(self) -> None:
        assert _any_window_contains(
            _REFERENCE.read_text(encoding="utf-8"),
            "pr ensure-pr",
            must_include="skip_for_classified",
            radius=400,
        ), "the reference must say why re-running `pr ensure-pr` on an open-PR branch is no heal"


class TestReferenceNamesTheAdoptLease:
    def test_reference_cites_the_lease_writer_its_ttl_and_the_dispatch_filter(self) -> None:
        text = _REFERENCE.read_text(encoding="utf-8")
        for token in (
            "teatree.core.models.external_delivery.mark_external_delivery",
            "teatree.core.models.external_delivery.LEASE_SECONDS",
            f"{LEASE_SECONDS} s",
            "teatree.core.models.task.Task.dispatchable_q",
        ):
            assert token in text, f"hook-opened-pr-ledger.md must name `{token}`"

    def test_reference_ties_the_lease_to_adopt(self) -> None:
        assert _any_window_contains(
            _REFERENCE.read_text(encoding="utf-8"),
            "--adopt",
            must_include="mark_external_delivery",
            radius=300,
        ), "the reference must say `--adopt` stamps the external-delivery lease"

    def test_reference_has_a_loop_agent_check_the_row_before_adopting(self) -> None:
        assert _any_window_contains(
            _REFERENCE.read_text(encoding="utf-8"),
            "mcp__teatree__worktree_status",
            must_include="--adopt",
            radius=300,
        ), "the reference must have a loop agent read the worktree row before running `--adopt`"


class TestReferenceNamesTheInstalledT3Hook:
    def test_reference_cites_the_isolated_db_predicate_the_wrapper_and_the_soft_refusal(self) -> None:
        text = _REFERENCE.read_text(encoding="utf-8")
        for token in (
            "teatree.core.provision.db_anchor.active_db_is_worktree_isolated",
            "scripts/hooks/ensure-pr-installed-t3.sh",
            "soft_refusal_commands",
        ):
            assert token in text, f"hook-opened-pr-ledger.md must name `{token}`"

    def test_reference_ties_each_skip_exit_to_the_wrapper(self) -> None:
        text = _REFERENCE.read_text(encoding="utf-8")
        for code in ("69", "75", "126", "127", "137"):
            assert _any_window_contains(text, "ensure-pr-installed-t3.sh", must_include=f"`{code}`", radius=900), (
                f"the reference must say the wrapper skips on exit `{code}`"
            )

    def test_reference_says_a_ship_push_sets_the_marker_because_it_holds_the_db_lock(self) -> None:
        text = _REFERENCE.read_text(encoding="utf-8")
        for token in (
            "teatree.core.forge_push.SHIP_PUSH_ENV",
            "ship_opens_pr=True",
            "BEGIN IMMEDIATE",
            "SKIP=ensure-pr",
        ):
            assert token in text, f"the reference must name `{token}`"

    def test_reference_names_the_shapes_the_hook_does_not_cover(self) -> None:
        text = _REFERENCE.read_text(encoding="utf-8")
        for token in ("detached HEAD", "t3 fast-push", "first ref", "uv run", "container", "no deadline"):
            assert token in text, f"the reference must name the uncovered shape `{token}`"


class TestPushersArePointedAtTheRule:
    def test_code_and_test_skills_point_at_4a1_with_the_adopt_flag(self) -> None:
        for skill in _POINTER_SKILLS:
            assert _any_window_contains(
                skill.read_text(encoding="utf-8"),
                "`skills/ship/SKILL.md` § 4a1",
                must_include="--adopt",
                radius=300,
            ), f"{skill.parent.name}/SKILL.md must point its first push at ship § 4a1 and `--adopt`"
