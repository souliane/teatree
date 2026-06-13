"""The CLEAR→MergeClear contract: issuance seam + sanctioned human-substrate merge.

#863 added the *consume* side (``t3 <overlay> ticket merge <clear_id>``) and the
prohibition guard, but left two gaps in the orchestrator-decides /
loop-executes topology (BLUEPRINT §17.4):

Gap 1 — No issuance seam. There was no ``t3`` command for an orchestrator to
record a per-diff CLEAR as a durable ``MergeClear`` row the loop can act on by
id. ``ticket clear`` is that seam (§17.4.2 — the orchestrator's only merge
output is a ``MergeClear`` row; §17.8 clause 3 — it must be independently
reviewed, so the issuer cannot equal the executing loop and a maker/loop role
cannot issue it).

Gap 2 — No sanctioned human-substrate merge. ``assert_merge_preconditions``
refuses ``blast_class == substrate`` unconditionally — correct for the loop,
but BLUEPRINT invariant 8 says even a human/owner merge must go through a
sanctioned ``t3`` path, never raw ``gh``. ``human_authorizer`` + ``ticket
merge --human-authorized`` is that path: a substrate CLEAR a human explicitly
authorised still merges through the same SHA-bound, audited transition, just
with the human decision recorded durably.

Only the unstoppable external — the ``gh`` subprocess — is stubbed; every
teatree model / FSM / DB write is real.
"""

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.merge import MergePreconditionError, assert_merge_preconditions, merge_ticket_pr
from teatree.core.models import MergeAudit, MergeClear, Ticket

pytestmark = pytest.mark.django_db

_SHA = "c" * 40
_GREEN = '[{"status": "COMPLETED", "conclusion": "SUCCESS"}]'


@contextmanager
def _overlay_autonomy(overlay: str, autonomy: str) -> Iterator[None]:
    """Stage a ``~/.teatree.toml`` setting ``overlay`` to ``autonomy`` and wire it in."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / ".teatree.toml"
        cfg.write_text(f'[teatree]\n[overlays.{overlay}]\nautonomy = "{autonomy}"\n', encoding="utf-8")
        with patch("teatree.config.CONFIG_PATH", cfg):
            yield


def _gh_stub(argv: list[str]) -> tuple[int, str, str]:
    joined = " ".join(argv)
    if "headRefOid" in joined:
        return (0, _SHA, "")
    if "isDraft" in joined:
        return (0, "false", "")
    if "statusCheckRollup" in joined:
        return (0, _GREEN, "")
    if "pulls" in joined and "merge" in joined:
        return (0, '{"sha": "landed00deadbeef"}', "")
    return (0, "", "")


class TestClearIssuanceSeam(TestCase):
    """``t3 ... ticket clear`` records the orchestrator's per-diff CLEAR."""

    def test_clear_creates_actionable_mergeclear_row(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        result = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "clear",
                "859",
                "souliane/teatree",
                reviewed_sha=_SHA,
                reviewer_identity="cold-reviewer",
                gh_verify_result="green",
                blast_class="docs",
                ticket_id=int(ticket.pk),
            ),
        )
        assert result["issued"]
        clear = MergeClear.objects.get(pk=result["clear_id"])
        assert clear.is_actionable()
        assert clear.reviewer_identity == "cold-reviewer"
        assert clear.reviewed_sha == _SHA
        assert clear.blast_class == MergeClear.BlastClass.DOCS
        assert clear.ticket_id == ticket.pk

    def test_clear_then_merge_round_trip(self) -> None:
        """The seam closes the loop: issue a CLEAR, the loop merges by its id."""
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        issued = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "clear",
                "861",
                "souliane/teatree",
                reviewed_sha=_SHA,
                reviewer_identity="cold-reviewer",
                gh_verify_result="green",
                blast_class="logic",
                ticket_id=int(ticket.pk),
            ),
        )
        with patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_gh_stub):
            merged = cast(
                "dict[str, object]",
                call_command("ticket", "merge", str(issued["clear_id"]), loop_identity="merge-loop"),
            )
        ticket.refresh_from_db()
        assert merged["merged"]
        assert ticket.state == Ticket.State.MERGED

    def test_clear_issuer_equal_to_executing_loop_is_refused(self) -> None:
        """§17.8 clause 3: a CLEAR cannot be issued by the loop that will execute it."""
        result = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "clear",
                "862",
                "souliane/teatree",
                reviewed_sha=_SHA,
                reviewer_identity="merge-loop",
                gh_verify_result="green",
                blast_class="docs",
            ),
        )
        assert not result["issued"]
        assert "merge-loop" in result["error"]
        assert MergeClear.objects.count() == 0

    def test_clear_with_maker_reviewer_is_refused(self) -> None:
        """A maker/coding-agent/loop role cannot self-attest its own review."""
        for maker in ("maker:coding", "coding-agent", "loop"):
            with self.subTest(maker=maker):
                result = cast(
                    "dict[str, object]",
                    call_command(
                        "ticket",
                        "clear",
                        "863",
                        "souliane/teatree",
                        reviewed_sha=_SHA,
                        reviewer_identity=maker,
                        gh_verify_result="green",
                        blast_class="docs",
                    ),
                )
                assert not result["issued"]
                assert "reviewer" in result["error"].lower()
        assert MergeClear.objects.count() == 0

    def test_clear_rejects_unknown_blast_class(self) -> None:
        result = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "clear",
                "864",
                "souliane/teatree",
                reviewed_sha=_SHA,
                reviewer_identity="cold-reviewer",
                gh_verify_result="green",
                blast_class="enormous",
            ),
        )
        assert not result["issued"]
        assert "blast_class" in result["error"]
        assert MergeClear.objects.count() == 0

    def test_clear_with_unknown_ticket_id_is_refused(self) -> None:
        result = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "clear",
                "868",
                "souliane/teatree",
                reviewed_sha=_SHA,
                reviewer_identity="cold-reviewer",
                gh_verify_result="green",
                blast_class="docs",
                ticket_id=999999,
            ),
        )
        assert not result["issued"]
        assert "not found" in result["error"]
        assert MergeClear.objects.count() == 0

    def test_clear_rejects_unknown_gh_verify_result(self) -> None:
        result = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "clear",
                "866",
                "souliane/teatree",
                reviewed_sha=_SHA,
                reviewer_identity="cold-reviewer",
                gh_verify_result="maybe",
                blast_class="docs",
            ),
        )
        assert not result["issued"]
        assert "gh_verify_result" in result["error"]
        assert MergeClear.objects.count() == 0

    def test_clear_rejects_empty_reviewer_identity(self) -> None:
        result = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "clear",
                "867",
                "souliane/teatree",
                reviewed_sha=_SHA,
                reviewer_identity="   ",
                gh_verify_result="green",
                blast_class="docs",
            ),
        )
        assert not result["issued"]
        assert "reviewer_identity is required" in result["error"]
        assert MergeClear.objects.count() == 0

    def test_clear_rejects_branch_ref_instead_of_sha(self) -> None:
        """``reviewed_sha`` binds to an exact tree — a branch ref is not a SHA."""
        result = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "clear",
                "865",
                "souliane/teatree",
                reviewed_sha="main",
                reviewer_identity="cold-reviewer",
                gh_verify_result="green",
                blast_class="docs",
            ),
        )
        assert not result["issued"]
        assert "sha" in result["error"].lower()
        assert MergeClear.objects.count() == 0

    def test_clear_normalizes_mixed_case_sha_to_lowercase(self) -> None:
        """Mixed-case 40-char SHAs are accepted but stored in canonical lowercase (#1162).

        The merge-time gate compares against GitHub's lowercase ``headRefOid``
        — persisting the mixed-case input verbatim would produce the same
        silent-failure mode this issue closes for truncated SHAs.
        """
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        mixed_case = "ABCDEF1234567890abcdef1234567890ABCDEF12"
        result = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "clear",
                "1163",
                "souliane/teatree",
                reviewed_sha=mixed_case,
                reviewer_identity="cold-reviewer",
                gh_verify_result="green",
                blast_class="docs",
                ticket_id=int(ticket.pk),
            ),
        )
        assert result["issued"]
        clear = MergeClear.objects.get(pk=result["clear_id"])
        assert clear.reviewed_sha == mixed_case.lower()

    def test_clear_rejects_truncated_sha_with_actionable_diagnostic(self) -> None:
        """A short hex SHA (#1162) is unmergeable from birth — refuse at issuance.

        The merge-time gate compares ``reviewed_sha`` against the full 40-char
        ``headRefOid`` from ``gh pr view``. A truncated SHA can never satisfy
        that equality — the CLEAR would be unusable on day one. The diagnostic
        must tell the operator what was passed (truncated form + length), what
        is required (full 40-char SHA), and where to find it.
        """
        truncated = "abc1234"
        result = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "clear",
                "1162",
                "souliane/teatree",
                reviewed_sha=truncated,
                reviewer_identity="cold-reviewer",
                gh_verify_result="green",
                blast_class="docs",
            ),
        )
        assert not result["issued"]
        error = cast("str", result["error"])
        assert truncated in error
        assert f"length={len(truncated)}" in error
        assert "40" in error
        assert "git rev-parse HEAD" in error or "headRefOid" in error
        assert MergeClear.objects.count() == 0

    def test_clear_accepts_reviewed_sha_as_named_option(self) -> None:
        """#1231: ``--reviewed-sha`` is the canonical named flag.

        Positional invocation was a footgun — operators sliced the SHA
        between ``slug`` and the named ``--reviewer-identity`` flag and
        produced unreadable command lines during keystone clear+merge
        sessions. The named form makes every CLEAR field a named flag,
        consistent with the rest of the surface.
        """
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        result = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "clear",
                "1231",
                "souliane/teatree",
                reviewed_sha=_SHA,
                reviewer_identity="cold-reviewer",
                gh_verify_result="green",
                blast_class="docs",
                ticket_id=int(ticket.pk),
            ),
        )
        assert result["issued"]
        clear = MergeClear.objects.get(pk=result["clear_id"])
        assert clear.reviewed_sha == _SHA

    def test_clear_without_reviewed_sha_exits_nonzero(self) -> None:
        """#1231: omitting ``--reviewed-sha`` exits non-zero so shell automation sees the failure.

        The option needs a default for ``call_command`` to accept it as a
        kwarg (django-typer requires defaults on ``Annotated`` options),
        so the runtime check is what makes the flag effectively required.
        Codex flagged the soft-fail variant: a zero-exit refusal hid the
        failure from any script piping the CLEAR through `&&`. Same
        ``stderr + SystemExit(1)`` shape as the sibling refusals in this
        command (`_resolve_ticket`, `context_add`).
        """
        with pytest.raises(SystemExit) as excinfo:
            call_command(
                "ticket",
                "clear",
                "1232",
                "souliane/teatree",
                reviewer_identity="cold-reviewer",
                gh_verify_result="green",
                blast_class="docs",
            )
        assert excinfo.value.code == 1
        assert MergeClear.objects.count() == 0


class TestSubstrateStaysHumanMergeOnly(TestCase):
    """The loop never auto-merges substrate; an un-authorised substrate CLEAR holds."""

    def test_substrate_clear_without_human_authorizer_is_held(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = MergeClear.objects.create(
            ticket=ticket,
            pr_id=870,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            reviewer_identity="cold-reviewer",
            gh_verify_result=MergeClear.VerifyResult.GREEN,
            blast_class=MergeClear.BlastClass.SUBSTRATE,
        )
        with (
            patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_gh_stub),
            pytest.raises(MergePreconditionError, match="substrate"),
        ):
            merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW
        assert not MergeAudit.objects.filter(clear=clear).exists()

    def test_clear_command_can_record_human_authorizer_for_substrate(self) -> None:
        result = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "clear",
                "871",
                "souliane/teatree",
                reviewed_sha=_SHA,
                reviewer_identity="cold-reviewer",
                gh_verify_result="green",
                blast_class="substrate",
                human_authorize="owner:adrien",
            ),
        )
        assert result["issued"]
        clear = MergeClear.objects.get(pk=result["clear_id"])
        assert clear.human_authorizer == "owner:adrien"

    def test_human_authorize_rejected_for_non_substrate(self) -> None:
        """``--human-authorize`` is meaningless off the substrate path — reject it loudly."""
        result = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "clear",
                "872",
                "souliane/teatree",
                reviewed_sha=_SHA,
                reviewer_identity="cold-reviewer",
                gh_verify_result="green",
                blast_class="logic",
                human_authorize="owner:adrien",
            ),
        )
        assert not result["issued"]
        assert "substrate" in result["error"]
        assert MergeClear.objects.count() == 0


class TestSanctionedHumanSubstrateMerge(TestCase):
    """A human-authorised substrate CLEAR merges through the SAME t3 transition."""

    def test_human_authorized_substrate_merges_and_records_authorizer(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = MergeClear.objects.create(
            ticket=ticket,
            pr_id=873,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            reviewer_identity="cold-reviewer",
            gh_verify_result=MergeClear.VerifyResult.GREEN,
            blast_class=MergeClear.BlastClass.SUBSTRATE,
            human_authorizer="owner:adrien",
        )
        with patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_gh_stub):
            result = cast(
                "dict[str, object]",
                call_command(
                    "ticket",
                    "merge",
                    str(clear.pk),
                    loop_identity="merge-loop",
                    human_authorized="owner:adrien",
                ),
            )
        ticket.refresh_from_db()
        clear.refresh_from_db()
        assert result["merged"]
        assert ticket.state == Ticket.State.MERGED
        assert clear.consumed_at is not None
        audit = MergeAudit.objects.get(clear=clear)
        assert audit.required_checks_status == "green"

    def test_substrate_merge_without_human_authorized_flag_is_held(self) -> None:
        """Even an authorised CLEAR will not auto-merge: the human flag is mandatory at execute time."""
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = MergeClear.objects.create(
            ticket=ticket,
            pr_id=874,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            reviewer_identity="cold-reviewer",
            gh_verify_result=MergeClear.VerifyResult.GREEN,
            blast_class=MergeClear.BlastClass.SUBSTRATE,
            human_authorizer="owner:adrien",
        )
        with patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_gh_stub):
            result = cast("dict[str, object]", call_command("ticket", "merge", str(clear.pk)))
        ticket.refresh_from_db()
        assert result["escalated"]
        assert not result["merged"]
        assert ticket.state == Ticket.State.IN_REVIEW

    def test_human_authorized_flag_must_match_recorded_authorizer(self) -> None:
        """The execute-time human flag must match the CLEAR's recorded authoriser."""
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = MergeClear.objects.create(
            ticket=ticket,
            pr_id=875,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            reviewer_identity="cold-reviewer",
            gh_verify_result=MergeClear.VerifyResult.GREEN,
            blast_class=MergeClear.BlastClass.SUBSTRATE,
            human_authorizer="owner:adrien",
        )
        with patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_gh_stub):
            result = cast(
                "dict[str, object]",
                call_command(
                    "ticket",
                    "merge",
                    str(clear.pk),
                    human_authorized="someone-else",
                ),
            )
        ticket.refresh_from_db()
        assert result["escalated"]
        assert not result["merged"]
        assert ticket.state == Ticket.State.IN_REVIEW

    def test_human_authorized_flag_on_non_substrate_clear_is_refused(self) -> None:
        """The human-substrate escape hatch must not be usable to bypass loop review of logic PRs."""
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = MergeClear.objects.create(
            ticket=ticket,
            pr_id=876,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            reviewer_identity="cold-reviewer",
            gh_verify_result=MergeClear.VerifyResult.GREEN,
            blast_class=MergeClear.BlastClass.LOGIC,
        )
        with patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_gh_stub):
            result = cast(
                "dict[str, object]",
                call_command(
                    "ticket",
                    "merge",
                    str(clear.pk),
                    human_authorized="owner:adrien",
                ),
            )
        ticket.refresh_from_db()
        assert result["escalated"]
        assert not result["merged"]
        assert ticket.state == Ticket.State.IN_REVIEW


class TestAgentExecutesApprovedSubstrateMerge(TestCase):
    """Binding correction (#836): approval is the gate; the AGENT executes the merge.

    The user operates write-only and cannot perform a merge action. The
    sanctioned substrate path is therefore: a human records an explicit
    approval (``MergeClear.human_authorizer``) at CLEAR-issue time, and the
    AGENT then executes the merge via the ``t3 ... ticket merge <clear_id>
    --human-authorized <id>`` CLI — the same callable the durable loop runs.
    These tests assert no code path requires a human to *perform* the merge
    action; ``human_authorizer`` is a recorded approval identity, never an
    actor gate.
    """

    def test_agent_cli_invocation_executes_the_approved_substrate_merge(self) -> None:
        """The merge runs through the ordinary agent CLI path — no human actor step."""
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = MergeClear.objects.create(
            ticket=ticket,
            pr_id=877,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            reviewer_identity="cold-reviewer",
            gh_verify_result=MergeClear.VerifyResult.GREEN,
            blast_class=MergeClear.BlastClass.SUBSTRATE,
            human_authorizer="owner:adrien",
        )
        # call_command is exactly what the durable loop / agent invokes for
        # `t3 <overlay> ticket merge`. No human-actor parameter exists; the
        # recorded approver is re-presented, the agent performs the merge.
        with patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_gh_stub):
            result = cast(
                "dict[str, object]",
                call_command(
                    "ticket",
                    "merge",
                    str(clear.pk),
                    loop_identity="merge-loop",
                    human_authorized="owner:adrien",
                ),
            )
        ticket.refresh_from_db()
        clear.refresh_from_db()
        assert result["merged"] is True
        assert ticket.state == Ticket.State.MERGED
        assert clear.consumed_at is not None
        # The approval identity is durably recorded — it is the gate, not the actor.
        assert clear.human_authorizer == "owner:adrien"

    def test_merge_executor_signature_has_no_human_actor_parameter(self) -> None:
        """Structural guarantee: the keystone executor takes no 'a human performs it' arg.

        Its only human-related parameter is ``human_authorized`` — the recorded
        *approval* id re-presented for verification. There is no parameter
        whose presence means 'a human, not the agent, performs the merge'.
        """
        import inspect  # noqa: PLC0415

        params = set(inspect.signature(merge_ticket_pr).parameters)
        assert params == {"clear", "executing_loop_identity", "human_authorized"}


class TestPrMergeRedirectedToKeystone(TestCase):
    """The old ``t3 ... pr merge`` path is FSM-incoherent post-#863 and must refuse."""

    def test_pr_merge_refuses_and_points_at_keystone(self) -> None:
        result = cast(
            "dict[str, object]",
            call_command("pr", "merge", "859", "souliane/teatree"),
        )
        assert not result["merged"]
        assert "ticket merge" in result["error"]
        assert "ticket clear" in result["error"]


def _substrate_clear(ticket: Ticket, **overrides: object) -> MergeClear:
    defaults: dict[str, object] = {
        "ticket": ticket,
        "pr_id": 1730,
        "slug": "souliane/teatree",
        "reviewed_sha": _SHA,
        "reviewer_identity": "cold-reviewer",
        "gh_verify_result": MergeClear.VerifyResult.GREEN,
        "blast_class": MergeClear.BlastClass.SUBSTRATE,
    }
    defaults.update(overrides)
    return MergeClear.objects.create(**defaults)


def _assert_preconditions(clear: MergeClear, *, human_authorized: str = "", slug: str | None = None) -> object:
    with patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_gh_stub):
        return assert_merge_preconditions(
            clear=clear,
            executing_loop_identity="merge-loop",
            slug=clear.slug if slug is None else slug,
            pr_id=clear.pr_id,
            human_authorized=human_authorized,
        )


class TestFullAutonomyStandingGrantSatisfiesSubstrateSignoff(TestCase):
    """Invariant 4 carve-out: an overlay at ``autonomy = full`` is the standing human approval.

    The substrate per-PR sign-off is satisfied by EITHER a per-CLEAR
    ``human_authorizer`` OR the CLEAR's overlay standing at ``autonomy = full``.
    The quality/safety floor (independent cold-review, reviewed-SHA bind,
    CI-green, not-draft, maker≠checker) is never relaxed by this knob — only
    the per-PR human sign-off is what ``full`` removes.
    """

    def test_full_autonomy_substrate_passes_without_human_authorizer(self) -> None:
        """MUST-ALLOW: full + substrate + reviewer!=maker + green + not-draft + no authorizer."""
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _substrate_clear(ticket)
        with _overlay_autonomy("t3-teatree", "full"):
            precheck = _assert_preconditions(clear)
        assert precheck is not None

    def test_full_autonomy_substrate_merges_end_to_end_without_human_authorizer(self) -> None:
        """MUST-ALLOW end-to-end: the keystone merge advances the FSM with no per-PR sign-off."""
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _substrate_clear(ticket, pr_id=1731)
        with (
            _overlay_autonomy("t3-teatree", "full"),
            patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_gh_stub),
        ):
            result = cast(
                "dict[str, object]",
                call_command("ticket", "merge", str(clear.pk), loop_identity="merge-loop"),
            )
        ticket.refresh_from_db()
        clear.refresh_from_db()
        assert result["merged"]
        assert ticket.state == Ticket.State.MERGED
        assert clear.consumed_at is not None

    def test_notify_autonomy_substrate_without_authorizer_still_refused(self) -> None:
        """MUST-DENY: notify (not full) keeps the per-PR human authorizer mandatory."""
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _substrate_clear(ticket, pr_id=1732)
        with _overlay_autonomy("t3-teatree", "notify"), pytest.raises(MergePreconditionError, match="substrate"):
            _assert_preconditions(clear)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW

    def test_babysit_autonomy_substrate_without_authorizer_still_refused(self) -> None:
        """MUST-DENY: babysit (the default) keeps the per-PR human authorizer mandatory."""
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _substrate_clear(ticket, pr_id=1733)
        with _overlay_autonomy("t3-teatree", "babysit"), pytest.raises(MergePreconditionError, match="substrate"):
            _assert_preconditions(clear)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW

    def test_full_autonomy_does_not_relax_maker_checker_floor(self) -> None:
        """MUST-DENY: full + reviewer==maker still refuses — the maker≠checker floor is intact."""
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _substrate_clear(ticket, pr_id=1734, reviewer_identity="coding-agent")
        with (
            _overlay_autonomy("t3-teatree", "full"),
            pytest.raises(MergePreconditionError, match=r"non-reviewer role|independent cold reviewer"),
        ):
            _assert_preconditions(clear)

    def test_full_autonomy_does_not_relax_sha_bind_floor(self) -> None:
        """MUST-DENY: full + head moved off reviewed_sha still refuses — the SHA bind is intact."""
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _substrate_clear(ticket, pr_id=1735, reviewed_sha="d" * 40)
        with _overlay_autonomy("t3-teatree", "full"), pytest.raises(MergePreconditionError, match="head moved"):
            _assert_preconditions(clear)

    def test_full_autonomy_does_not_relax_ci_green_floor(self) -> None:
        """MUST-DENY: full + non-green recorded verdict still refuses — the CI floor is intact."""
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _substrate_clear(ticket, pr_id=1736, gh_verify_result=MergeClear.VerifyResult.FAILED)
        with _overlay_autonomy("t3-teatree", "full"), pytest.raises(MergePreconditionError, match="not green"):
            _assert_preconditions(clear)

    def test_full_autonomy_does_not_relax_not_draft_floor(self) -> None:
        """MUST-DENY: full + draft PR still refuses — the not-draft floor is intact."""
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _substrate_clear(ticket, pr_id=1737)

        def _draft_stub(argv: list[str]) -> tuple[int, str, str]:
            joined = " ".join(argv)
            if "isDraft" in joined:
                return (0, "true", "")
            return _gh_stub(argv)

        with (
            _overlay_autonomy("t3-teatree", "full"),
            patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_draft_stub),
            pytest.raises(MergePreconditionError, match="draft"),
        ):
            assert_merge_preconditions(
                clear=clear,
                executing_loop_identity="merge-loop",
                slug=clear.slug,
                pr_id=clear.pr_id,
            )

    def test_full_on_other_overlay_does_not_leak(self) -> None:
        """MUST-DENY: full on overlay A never relaxes substrate on overlay B (B stays gated)."""
        ticket = Ticket.objects.create(overlay="t3-client", state=Ticket.State.IN_REVIEW)
        clear = _substrate_clear(ticket, pr_id=1738)
        with _overlay_autonomy("t3-teatree", "full"), pytest.raises(MergePreconditionError, match="substrate"):
            _assert_preconditions(clear)

    def test_per_clear_human_authorizer_still_works_under_babysit(self) -> None:
        """A matching per-CLEAR ``human_authorizer`` is the unchanged path for non-full overlays."""
        ticket = Ticket.objects.create(overlay="t3-client", state=Ticket.State.IN_REVIEW)
        clear = _substrate_clear(ticket, pr_id=1739, human_authorizer="owner:adrien")
        with _overlay_autonomy("t3-client", "babysit"):
            precheck = _assert_preconditions(clear, human_authorized="owner:adrien")
        assert precheck is not None

    def test_full_autonomy_substrate_passes_for_ticketless_clear_via_slug(self) -> None:
        """MUST-ALLOW: a ticket-less CLEAR resolves its overlay from ``slug`` (the loop's common case)."""
        clear = _substrate_clear(None, pr_id=1740, slug="souliane/teatree")
        with _overlay_autonomy("t3-teatree", "full"):
            precheck = _assert_preconditions(clear)
        assert precheck is not None

    def test_full_autonomy_substrate_passes_when_ticket_overlay_is_canonical_alias(self) -> None:
        """MUST-ALLOW: ``ticket.overlay`` short alias resolves the entry-point-keyed override."""
        ticket = Ticket.objects.create(overlay="teatree", state=Ticket.State.IN_REVIEW)
        clear = _substrate_clear(ticket, pr_id=1741)
        with _overlay_autonomy("t3-teatree", "full"):
            precheck = _assert_preconditions(clear)
        assert precheck is not None

    def test_babysit_autonomy_ticketless_clear_still_refused(self) -> None:
        """MUST-DENY: a ticket-less CLEAR under a below-full overlay keeps the per-PR sign-off."""
        clear = _substrate_clear(None, pr_id=1742, slug="souliane/teatree")
        with _overlay_autonomy("t3-teatree", "babysit"), pytest.raises(MergePreconditionError, match="substrate"):
            _assert_preconditions(clear)

    def test_full_autonomy_ticketless_clear_unresolvable_slug_refused(self) -> None:
        """MUST-DENY: a ticket-less CLEAR whose slug maps to no overlay fails closed."""
        clear = _substrate_clear(None, pr_id=1743, slug="unknown-owner/unknown-repo")
        with _overlay_autonomy("t3-teatree", "full"), pytest.raises(MergePreconditionError, match="substrate"):
            _assert_preconditions(clear)


class TestBranchSlugClearResolvesOverlayFromRecoveredRepo(TestCase):
    """A branch-name-slug CLEAR resolves its overlay from the merge's recovered repo.

    The loop issues ticket-less substrate CLEARs whose stored ``slug`` is a
    *branch name* (``merge-candidate-working-repos``), not ``owner/repo``. The
    merge keystone recovers the real ``owner/repo`` via
    ``resolve_pr_repo_slug`` → ``_reconcile_slug_against_reviewed_sha`` and
    threads it into ``assert_merge_preconditions`` as the ``slug`` kwarg. The
    autonomy carve-out must resolve its overlay from that recovered slug — not
    the raw branch-name slug, which maps to no overlay (the global babysit
    default) and wrongly refuses a merge the overlay genuinely stands ``full``
    for. Fail-closed is preserved: a branch-slug CLEAR whose recovered repo is
    NOT full (or unresolvable) still requires the per-PR human authoriser.
    """

    def test_full_autonomy_branch_slug_passes_via_recovered_repo(self) -> None:
        """MUST-ALLOW: branch-name slug + recovered owner/repo at full satisfies the sign-off.

        RED before the fix: the carve-out resolved the overlay from the raw
        branch-name ``slug`` (→ no overlay → babysit) and refused with
        "the overlay autonomy is not full" despite the recovered repo's overlay
        standing at full.
        """
        clear = _substrate_clear(None, pr_id=1750, slug="merge-candidate-working-repos")
        with _overlay_autonomy("t3-teatree", "full"):
            precheck = _assert_preconditions(clear, slug="souliane/teatree")
        assert precheck is not None

    def test_full_autonomy_branch_slug_without_recovered_repo_still_refused(self) -> None:
        """MUST-DENY: a branch-name slug with no recovered owner/repo fails closed.

        When the merge cannot recover a real repo (the recovered slug is still
        the branch name), the overlay is unresolvable — fail-closed: the per-PR
        human authoriser stays mandatory even under a full overlay.
        """
        clear = _substrate_clear(None, pr_id=1751, slug="merge-candidate-working-repos")
        with _overlay_autonomy("t3-teatree", "full"), pytest.raises(MergePreconditionError, match="substrate"):
            _assert_preconditions(clear, slug="merge-candidate-working-repos")

    def test_branch_slug_recovered_repo_at_babysit_still_refused(self) -> None:
        """MUST-DENY: branch slug recovers a real repo, but its overlay is below full.

        The recovery succeeds (the recovered ``owner/repo`` maps to ``t3-teatree``),
        but the overlay stands at babysit — fail-closed: the carve-out fires only
        for a genuinely-full overlay, so the per-PR human authoriser stays mandatory.
        This is the no-silent-widening guard: recovery never lowers the floor.
        """
        clear = _substrate_clear(None, pr_id=1752, slug="merge-candidate-working-repos")
        with _overlay_autonomy("t3-teatree", "babysit"), pytest.raises(MergePreconditionError, match="substrate"):
            _assert_preconditions(clear, slug="souliane/teatree")

    def test_branch_slug_recovered_repo_maps_to_no_overlay_refused(self) -> None:
        """MUST-DENY: a branch slug whose recovered repo maps to no overlay fails closed."""
        clear = _substrate_clear(None, pr_id=1753, slug="merge-candidate-working-repos")
        with _overlay_autonomy("t3-teatree", "full"), pytest.raises(MergePreconditionError, match="substrate"):
            _assert_preconditions(clear, slug="unknown-owner/unknown-repo")
