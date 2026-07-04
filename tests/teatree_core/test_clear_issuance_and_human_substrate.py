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

from teatree.core.merge import MergePreconditionError, assert_merge_preconditions, merge_ticket_pr, resolve_pr_repo_slug
from teatree.core.merge.authorization import assert_review_verdict_gate
from teatree.core.models import ConfigSetting, MergeAudit, MergeClear, ReviewVerdict, Ticket
from tests.teatree_core.conftest import seed_merge_safe_verdict

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _skip_author_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    # #1773 public-repo author gate — exercised by test_merge_execution_author_gate;
    # these pre-date it and target other concerns, so it is a no-op here.
    monkeypatch.setattr("teatree.core.merge.execution.assert_public_repo_author_trusted", lambda **_: None)


_SHA = "c" * 40
_GREEN = '[{"status": "COMPLETED", "conclusion": "SUCCESS"}]'


@contextmanager
def _overlay_autonomy(overlay: str, autonomy: str) -> Iterator[None]:
    """Stage ``overlay``'s ``autonomy`` and wire it in.

    ``autonomy`` is DB-home (#1775, no ``T3_*`` env var) so a ``[overlays.<n>]``
    TOML value for it is ignored on read — stage it in the ``ConfigSetting``
    store scoped to the overlay. ``CONFIG_PATH`` is still pinned at an empty
    TOML so the developer's real config is never read.
    """
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / ".teatree.toml"
        cfg.write_text("[teatree]\n", encoding="utf-8")
        row = ConfigSetting.objects.set_value("autonomy", autonomy, scope=overlay)
        try:
            with patch("teatree.config.CONFIG_PATH", cfg):
                yield
        finally:
            row.delete()


@contextmanager
def _overlay_standing_signoff(overlay: str, *, autonomy: str, require_human_approval_to_merge: bool) -> Iterator[None]:
    """Stage ``overlay``'s ``autonomy`` AND ``require_human_approval_to_merge`` together.

    Both are DB-home (#1775) so they are staged as ``ConfigSetting`` rows scoped
    to the overlay. This wires the canonical documented auto-merge knobs
    (``mode = auto`` is collapsed in by ``autonomy``/the resolver) so a test can
    pin the exact owner statement: e.g. ``autonomy = babysit`` (default, never
    flipped) + ``require_human_approval_to_merge = false`` (the owner's explicit
    "no per-PR human merge approval" grant).
    """
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / ".teatree.toml"
        cfg.write_text("[teatree]\n", encoding="utf-8")
        autonomy_row = ConfigSetting.objects.set_value("autonomy", autonomy, scope=overlay)
        approval_row = ConfigSetting.objects.set_value(
            "require_human_approval_to_merge", require_human_approval_to_merge, scope=overlay
        )
        try:
            with patch("teatree.config.CONFIG_PATH", cfg):
                yield
        finally:
            approval_row.delete()
            autonomy_row.delete()


def _gh_stub(argv: list[str]) -> tuple[int, str, str]:
    joined = " ".join(argv)
    if "headRefOid" in joined:
        return (0, _SHA, "")
    if "isDraft" in joined:
        return (0, "false", "")
    if "statusCheckRollup" in joined:
        return (0, _GREEN, "")
    if "baseRefName" in joined or "required_status_checks" in joined:
        # Base branch "main"; empty required-context gate → live rollup verdict stands.
        return (0, "main" if "baseRefName" in joined else '{"contexts": []}', "")
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
        seed_merge_safe_verdict(slug=clear.slug, pr_id=clear.pr_id, sha=clear.reviewed_sha)
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
        seed_merge_safe_verdict(slug=clear.slug, pr_id=clear.pr_id, sha=clear.reviewed_sha)
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

        Its human-related parameters are ``human_authorized`` (substrate approval)
        and ``expedite_authorized`` (PENDING-checks waiver) — each a recorded
        *approval* id re-presented for verification. There is no parameter whose
        presence means 'a human, not the agent, performs the merge'.
        """
        import inspect  # noqa: PLC0415

        params = set(inspect.signature(merge_ticket_pr).parameters)
        assert params == {"clear", "executing_loop_identity", "human_authorized", "expedite_authorized"}


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
    clear = MergeClear.objects.create(**defaults)
    # The #2829 merge-verdict gate needs the sibling verdict the real clear path
    # records (harmless on the held-substrate tests that refuse before the gate).
    seed_merge_safe_verdict(slug=clear.slug, pr_id=clear.pr_id, sha=clear.reviewed_sha)
    return clear


def _assert_preconditions(clear: MergeClear, *, human_authorized: str = "", slug: str | None = None) -> object:
    with patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_gh_stub):
        return assert_merge_preconditions(
            clear=clear,
            executing_loop_identity="merge-loop",
            slug=clear.slug if slug is None else slug,
            pr_id=clear.pr_id,
            human_authorized=human_authorized,
        )


class TestFullAutonomySubstrateIsHeldAndPingedNotAutoMerged(TestCase):
    """Under ``autonomy = full`` a SUBSTRATE clear is HELD for the owner, never auto-merged.

    The owner's directive: substrate (merge keystone, architecture spec,
    governance doc) must PING-and-HOLD so the owner sees and authorizes every
    such merge — even at ``autonomy = full``. The standing grant removes the
    per-PR human sign-off only for NON-substrate changes; a substrate clear under
    ``full`` raises the same ``MergePreconditionError`` (routed at the loop edge to
    the substrate-hold Slack ping). The only path that merges a substrate clear is
    a per-CLEAR ``human_authorizer`` re-presented at merge time. The quality/safety
    floor (independent cold-review, reviewed-SHA bind, CI-green, not-draft,
    maker≠checker) is untouched.
    """

    def test_full_autonomy_substrate_is_held_without_human_authorizer(self) -> None:
        """MUST-DENY: full + substrate + no authorizer is HELD (the standing grant excludes substrate)."""
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _substrate_clear(ticket)
        with _overlay_autonomy("t3-teatree", "full"), pytest.raises(MergePreconditionError, match="substrate"):
            _assert_preconditions(clear)

    def test_full_autonomy_substrate_is_held_end_to_end_no_auto_merge(self) -> None:
        """MUST-DENY end-to-end: the keystone merge HOLDS the substrate clear and leaves the FSM untouched.

        Pins the regression the fix closes: before the fix, ``autonomy = full``
        auto-merged this substrate clear SILENTLY (``merged`` True, FSM advanced,
        no ping). Now it is held (``merged`` False, escalated) so the loop edge can
        ping the owner.
        """
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
        assert result["merged"] is False
        assert result["escalated"] is True
        assert result["escalation_kind"] == "substrate"
        assert ticket.state == Ticket.State.IN_REVIEW
        assert clear.consumed_at is None

    def test_full_autonomy_substrate_with_human_authorizer_still_merges(self) -> None:
        """MUST-ALLOW: a per-CLEAR ``human_authorizer`` re-presented at merge is the unchanged substrate path."""
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _substrate_clear(ticket, pr_id=1730, human_authorizer="owner:adrien")
        with _overlay_autonomy("t3-teatree", "full"):
            precheck = _assert_preconditions(clear, human_authorized="owner:adrien")
        assert precheck is not None

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

    def test_human_authorized_substrate_does_not_relax_sha_bind_floor(self) -> None:
        """MUST-DENY: a human-authorized substrate clear with head moved off reviewed_sha still refuses.

        The SHA-bind floor runs AFTER the substrate sign-off, so it is exercised
        with the per-CLEAR human authoriser present (the only substrate-merge
        path); the bind still fails closed on a moved head.
        """
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _substrate_clear(ticket, pr_id=1735, reviewed_sha="d" * 40, human_authorizer="owner:adrien")
        with _overlay_autonomy("t3-teatree", "full"), pytest.raises(MergePreconditionError, match="head moved"):
            _assert_preconditions(clear, human_authorized="owner:adrien")

    def test_full_autonomy_does_not_relax_ci_green_floor(self) -> None:
        """MUST-DENY: full + FAILED recorded verdict still refuses — the CI floor is intact."""
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _substrate_clear(ticket, pr_id=1736, gh_verify_result=MergeClear.VerifyResult.FAILED)
        with (
            _overlay_autonomy("t3-teatree", "full"),
            pytest.raises(MergePreconditionError, match="FAILED required check"),
        ):
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
        """MUST-DENY: full on overlay A never relaxes substrate on overlay B (B stays gated).

        The CLEAR targets a repo (``other-owner/other-repo``) that NO full
        overlay owns — repo identity resolves it to no full overlay, so
        ``t3-teatree`` standing full never leaks the carve-out onto it. (The
        repo identity, not the stored ``ticket.overlay``, is what scopes the
        carve-out — so this uses a foreign slug, not merely a foreign token on a
        t3-teatree-owned repo.)
        """
        ticket = Ticket.objects.create(overlay="t3-client", state=Ticket.State.IN_REVIEW)
        clear = _substrate_clear(ticket, pr_id=1738, slug="other-owner/other-repo")
        with _overlay_autonomy("t3-teatree", "full"), pytest.raises(MergePreconditionError, match="substrate"):
            _assert_preconditions(clear, slug="other-owner/other-repo")

    def test_per_clear_human_authorizer_still_works_under_babysit(self) -> None:
        """A matching per-CLEAR ``human_authorizer`` is the unchanged path for non-full overlays."""
        ticket = Ticket.objects.create(overlay="t3-client", state=Ticket.State.IN_REVIEW)
        clear = _substrate_clear(ticket, pr_id=1739, human_authorizer="owner:adrien")
        with _overlay_autonomy("t3-client", "babysit"):
            precheck = _assert_preconditions(clear, human_authorized="owner:adrien")
        assert precheck is not None

    def test_full_autonomy_substrate_is_held_for_ticketless_clear_via_slug(self) -> None:
        """MUST-DENY: a ticket-less CLEAR whose slug resolves to the full overlay is STILL held (substrate)."""
        clear = _substrate_clear(None, pr_id=1740, slug="souliane/teatree")
        with _overlay_autonomy("t3-teatree", "full"), pytest.raises(MergePreconditionError, match="substrate"):
            _assert_preconditions(clear)

    def test_full_autonomy_substrate_is_held_when_ticket_overlay_is_canonical_alias(self) -> None:
        """MUST-DENY: a ``ticket.overlay`` alias resolving to the full overlay is STILL held (substrate)."""
        ticket = Ticket.objects.create(overlay="teatree", state=Ticket.State.IN_REVIEW)
        clear = _substrate_clear(ticket, pr_id=1741)
        with _overlay_autonomy("t3-teatree", "full"), pytest.raises(MergePreconditionError, match="substrate"):
            _assert_preconditions(clear)

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
    """A branch-name-slug substrate CLEAR is held under ``full`` regardless of recovered repo.

    The loop issues ticket-less substrate CLEARs whose stored ``slug`` is a
    *branch name* (``merge-candidate-working-repos``), not ``owner/repo``. The
    merge keystone recovers the real ``owner/repo`` and threads it into
    ``assert_merge_preconditions`` as the ``slug`` kwarg. Substrate is HELD for the
    owner regardless of how the overlay resolves: even when the recovered repo's
    overlay stands at ``full``, a substrate CLEAR is held (ping-and-hold), never
    auto-merged. The recovered-repo overlay resolution still governs the NON-
    substrate standing grant (exercised elsewhere); for substrate it is moot.
    """

    def test_full_autonomy_branch_slug_substrate_held_via_recovered_repo(self) -> None:
        """MUST-DENY: a branch-name slug recovered to a full overlay's repo is STILL held (substrate)."""
        clear = _substrate_clear(None, pr_id=1750, slug="merge-candidate-working-repos")
        with _overlay_autonomy("t3-teatree", "full"), pytest.raises(MergePreconditionError, match="substrate"):
            _assert_preconditions(clear, slug="souliane/teatree")

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


# A generic SECOND tooling repo the bundled overlay owns alongside its own repo.
# The bundled overlay (``t3-teatree``) can govern more than one repo; this stands
# in for any such co-owned repo without naming a private downstream project.
_OWNED_TOOLING_REPO = "tooling-org/tooling-extra"
# A repo owned by NO registered (full) overlay in the test environment.
_FOREIGN_REPO = "foreign-org/foreign-product"


@contextmanager
def _teatree_owns(*repo_slugs: str) -> Iterator[None]:
    """Make the bundled ``t3-teatree`` overlay own *repo_slugs* (workspace repos).

    A second repo the bundled overlay governs is declared in its
    ``workspace_repos``; the empty test config leaves it on the bundled default,
    so the test pins the ownership explicitly.
    """
    from teatree.core.overlay_loader import get_all_overlays  # noqa: PLC0415

    teatree_overlay = get_all_overlays()["t3-teatree"]
    with patch.object(type(teatree_overlay), "get_workspace_repos", return_value=list(repo_slugs)):
        yield


class TestMergeGateResolvesOverlayByRepoIdentity(TestCase):
    """The merge-approval gate resolves by REPO IDENTITY, not the stored ticket overlay.

    A repo's OWNING overlay (the overlay whose ``workspace_repos`` declare it)
    decides the merge-approval gate. An overlay at ``autonomy = full`` merges its
    repos with no per-PR human sign-off; a below-full overlay keeps the sign-off
    mandatory. The name-collision trap: two overlays can carry similar names
    while owning disjoint repo sets, so the overlay TOKEN a ticket carries is not
    a reliable gate key — the repo's owner is.

    The bug this pins: a substrate CLEAR for a PR on a repo a ``full`` overlay
    owns, whose ticket was MIS-STAMPED with a *different* overlay token (the
    typed/active overlay at ticket creation, not the repo's owner), resolved its
    autonomy against the stamped token (below full) and refused the merge — even
    though the repo's owning overlay merges it freely. The resolver must let the
    repo's OWNING overlay (``infer_overlay_for_url``) win over the stored token.
    """

    def test_owned_repo_resolves_to_owning_overlay_despite_mis_stamped_ticket(self) -> None:
        """Repo identity wins over a mis-stamped ``ticket.overlay``.

        RED before the fix: ``_resolve_clear_overlay_name`` returned the stored
        ``ticket.overlay`` (a foreign token) first, so a PR on a repo the
        bundled overlay owns resolved to the foreign overlay instead.
        """
        from teatree.core.merge.authorization import _resolve_clear_overlay_name  # noqa: PLC0415

        ticket = Ticket.objects.create(
            overlay="some-other-overlay",  # mis-stamped: not the repo's owner
            issue_url=f"https://github.com/{_OWNED_TOOLING_REPO}/pull/5",
            state=Ticket.State.IN_REVIEW,
        )
        clear = _substrate_clear(ticket, pr_id=5, slug=_OWNED_TOOLING_REPO)
        with _teatree_owns("souliane/teatree", _OWNED_TOOLING_REPO):
            resolved = _resolve_clear_overlay_name(clear, resolved_slug=_OWNED_TOOLING_REPO)
        assert resolved == "t3-teatree"

    def test_owned_repo_substrate_pr_is_still_held(self) -> None:
        """A SUBSTRATE PR on a repo the full overlay owns is STILL held (ping-and-hold).

        Repo identity resolves to the ``full`` overlay, but substrate is held for
        the owner regardless — the standing grant excludes substrate. (The
        repo-identity standing grant still governs NON-substrate changes, pinned by
        ``test_owned_repo_logic_clear_is_covered_by_standing_grant``.)
        """
        ticket = Ticket.objects.create(
            overlay="some-other-overlay",
            issue_url=f"https://github.com/{_OWNED_TOOLING_REPO}/pull/6",
            state=Ticket.State.IN_REVIEW,
        )
        clear = _substrate_clear(ticket, pr_id=6, slug=_OWNED_TOOLING_REPO)
        with (
            _teatree_owns("souliane/teatree", _OWNED_TOOLING_REPO),
            _overlay_autonomy("t3-teatree", "full"),
            pytest.raises(MergePreconditionError, match="substrate"),
        ):
            _assert_preconditions(clear, slug=_OWNED_TOOLING_REPO)

    def test_owned_repo_logic_clear_is_covered_by_standing_grant(self) -> None:
        """The NON-substrate standing grant resolves by repo identity (``full`` overlay owns the repo).

        Tests ``_overlay_grants_standing_substrate_signoff`` directly for a LOGIC
        clear: the repo's owning overlay (resolved from its slug) standing at
        ``full`` covers the per-PR sign-off. (End-to-end, a logic clear never reaches
        the substrate branch — this pins the resolution + standing-grant logic.)
        """
        from teatree.core.merge.authorization import _overlay_grants_standing_substrate_signoff  # noqa: PLC0415

        ticket = Ticket.objects.create(
            overlay="some-other-overlay",
            issue_url=f"https://github.com/{_OWNED_TOOLING_REPO}/pull/6",
            state=Ticket.State.IN_REVIEW,
        )
        clear = _substrate_clear(ticket, pr_id=6, slug=_OWNED_TOOLING_REPO, blast_class=MergeClear.BlastClass.LOGIC)
        with _teatree_owns("souliane/teatree", _OWNED_TOOLING_REPO), _overlay_autonomy("t3-teatree", "full"):
            granted = _overlay_grants_standing_substrate_signoff(clear, resolved_slug=_OWNED_TOOLING_REPO)
        assert granted is True

    def test_foreign_repo_logic_clear_is_not_covered_by_standing_grant(self) -> None:
        """The NON-substrate standing grant does NOT cover a repo no full overlay owns.

        The anti-vacuity twin: a LOGIC clear on a foreign repo resolves to no full
        overlay, so ``_overlay_grants_standing_substrate_signoff`` returns False.
        """
        from teatree.core.merge.authorization import _overlay_grants_standing_substrate_signoff  # noqa: PLC0415

        ticket = Ticket.objects.create(
            overlay="some-other-overlay",
            issue_url=f"https://gitlab.com/{_FOREIGN_REPO}/-/merge_requests/7",
            state=Ticket.State.IN_REVIEW,
        )
        clear = _substrate_clear(ticket, pr_id=7, slug=_FOREIGN_REPO, blast_class=MergeClear.BlastClass.LOGIC)
        with _teatree_owns("souliane/teatree", _OWNED_TOOLING_REPO), _overlay_autonomy("t3-teatree", "full"):
            granted = _overlay_grants_standing_substrate_signoff(clear, resolved_slug=_FOREIGN_REPO)
        assert granted is False

    def test_substrate_clear_is_excluded_from_standing_grant(self) -> None:
        """A SUBSTRATE clear is never covered by the standing grant, even on a full-owned repo (§3.2)."""
        from teatree.core.merge.authorization import _overlay_grants_standing_substrate_signoff  # noqa: PLC0415

        ticket = Ticket.objects.create(
            overlay="some-other-overlay",
            issue_url=f"https://github.com/{_OWNED_TOOLING_REPO}/pull/8",
            state=Ticket.State.IN_REVIEW,
        )
        clear = _substrate_clear(ticket, pr_id=8, slug=_OWNED_TOOLING_REPO)
        with _teatree_owns("souliane/teatree", _OWNED_TOOLING_REPO), _overlay_autonomy("t3-teatree", "full"):
            granted = _overlay_grants_standing_substrate_signoff(clear, resolved_slug=_OWNED_TOOLING_REPO)
        assert granted is False


class TestRequireHumanApprovalFalseStandingGrantNonSubstrate(TestCase):
    """#2666: ``require_human_approval_to_merge = false`` is the SAME standing grant as ``autonomy = full``.

    The standing grant (``autonomy = full`` OR ``require_human_approval_to_merge =
    false`` on a non-collaborative tier) removes the per-PR human sign-off for
    NON-substrate changes — proven here with a ``logic`` CLEAR. SUBSTRATE is
    excluded from the standing grant entirely (held for the owner, ping-and-hold),
    pinned by ``test_require_false_substrate_is_still_held`` below.

    The quality/safety floor (independent cold-review, reviewed-SHA bind,
    CI-green, not-draft, maker≠checker) is never relaxed by this — only the
    per-PR human sign-off is what the standing grant removes.
    """

    def test_explicit_require_false_at_babysit_signs_off_non_substrate(self) -> None:
        """MUST-ALLOW: babysit + require_human_approval_to_merge=false signs off a NON-substrate CLEAR.

        RED before the #2666 fix: the standing-grant check keyed solely on
        ``autonomy = full``, so an explicit ``require_human_approval_to_merge =
        false`` at the default ``babysit`` tier was ignored.
        """
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _substrate_clear(ticket, pr_id=2660, blast_class=MergeClear.BlastClass.LOGIC)
        with _overlay_standing_signoff("t3-teatree", autonomy="babysit", require_human_approval_to_merge=False):
            precheck = _assert_preconditions(clear)
        assert precheck is not None

    def test_explicit_require_false_at_babysit_merges_non_substrate_end_to_end(self) -> None:
        """MUST-ALLOW end-to-end: the keystone merge advances the FSM for a NON-substrate CLEAR."""
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _substrate_clear(ticket, pr_id=2661, blast_class=MergeClear.BlastClass.LOGIC)
        with (
            _overlay_standing_signoff("t3-teatree", autonomy="babysit", require_human_approval_to_merge=False),
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

    def test_require_false_substrate_is_still_held(self) -> None:
        """MUST-DENY: require_human_approval_to_merge=false does NOT sign off a SUBSTRATE CLEAR.

        Substrate is excluded from the standing grant entirely — even with the
        owner's explicit ``require_human_approval_to_merge = false`` it is held for
        the owner (ping-and-hold).
        """
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _substrate_clear(ticket, pr_id=2666)
        with (
            _overlay_standing_signoff("t3-teatree", autonomy="babysit", require_human_approval_to_merge=False),
            pytest.raises(MergePreconditionError, match="substrate"),
        ):
            _assert_preconditions(clear)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW

    def test_default_require_true_at_babysit_still_refused(self) -> None:
        """MUST-DENY: babysit + require_human_approval_to_merge=true keeps the per-PR sign-off.

        The default posture (the owner has NOT declared a standing grant) keeps
        the substrate sign-off mandatory — the anti-vacuity twin of the
        must-ALLOW above.
        """
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _substrate_clear(ticket, pr_id=2662)
        with (
            _overlay_standing_signoff("t3-teatree", autonomy="babysit", require_human_approval_to_merge=True),
            pytest.raises(MergePreconditionError, match="substrate"),
        ):
            _assert_preconditions(clear)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW

    def test_notify_tier_with_require_false_still_refused(self) -> None:
        """MUST-DENY: the notify collaborative tier keeps the per-PR human authoriser mandatory.

        ``notify`` also collapses ``require_human_approval_to_merge`` to false,
        but a notify-tier MR merges only after a colleague approval, so its
        substrate CLEAR is NOT a self-owned standing grant — the per-PR human
        authoriser stays mandatory. This guards against reading the collapsed
        ``require_human_approval_to_merge = false`` as an owner statement when it
        is merely a tier side effect of the collaborative tier.
        """
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _substrate_clear(ticket, pr_id=2663)
        with (
            _overlay_autonomy("t3-teatree", "notify"),
            pytest.raises(MergePreconditionError, match="substrate"),
        ):
            _assert_preconditions(clear)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW

    def test_require_false_does_not_relax_maker_checker_floor(self) -> None:
        """MUST-DENY: require=false + reviewer==maker still refuses — the maker≠checker floor holds."""
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _substrate_clear(ticket, pr_id=2664, reviewer_identity="coding-agent")
        with (
            _overlay_standing_signoff("t3-teatree", autonomy="babysit", require_human_approval_to_merge=False),
            pytest.raises(MergePreconditionError, match=r"non-reviewer role|independent cold reviewer"),
        ):
            _assert_preconditions(clear)

    def test_require_false_does_not_relax_sha_bind_floor(self) -> None:
        """MUST-DENY: a human-authorized substrate clear with head moved still refuses — SHA bind holds.

        The SHA-bind floor runs after the substrate sign-off, so it is exercised
        with the per-CLEAR human authoriser present (substrate holds first
        otherwise); the bind still fails closed on a moved head.
        """
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _substrate_clear(ticket, pr_id=2665, reviewed_sha="d" * 40, human_authorizer="owner:adrien")
        with (
            _overlay_standing_signoff("t3-teatree", autonomy="babysit", require_human_approval_to_merge=False),
            pytest.raises(MergePreconditionError, match="head moved"),
        ):
            _assert_preconditions(clear, human_authorized="owner:adrien")


class TestClearCanonicalizesVerdictSlug(TestCase):
    """A bare/workstream CLEAR slug records the by-product verdict where the #2829 gate looks it up."""

    def test_bare_slug_clear_records_verdict_under_resolved_owner_repo(self) -> None:
        """`ticket clear` with a bare repo slug keys its verdict under ``resolve_pr_repo_slug``.

        Pre-fix the verdict was recorded under the raw ``clear.slug`` ("teatree"),
        but ``assert_review_verdict_gate`` looks it up under
        ``resolve_pr_repo_slug(clear)`` ("souliane/teatree") — so a bare slug was
        silently unmergeable ("no recorded merge_safe ReviewVerdict at the live
        head") even though the independent cold review WAS recorded.
        """
        ticket = Ticket.objects.create(
            overlay="t3-teatree",
            state=Ticket.State.IN_REVIEW,
            issue_url="https://github.com/souliane/teatree/issues/859",
        )
        result = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "clear",
                "859",
                "teatree",
                reviewed_sha=_SHA,
                reviewer_identity="cold-reviewer",
                gh_verify_result="green",
                blast_class="docs",
                ticket_id=int(ticket.pk),
            ),
        )
        assert result["issued"]
        clear = MergeClear.objects.get(pk=result["clear_id"])
        assert clear.slug == "teatree"

        resolved = resolve_pr_repo_slug(clear)
        assert resolved == "souliane/teatree"

        verdict = ReviewVerdict.objects.get(pk=result["recorded_verdict_id"])
        assert verdict.slug == resolved

        assert_review_verdict_gate(slug=resolved, pr_id=clear.pr_id, head_sha=clear.reviewed_sha)

    def test_qualified_slug_clear_records_verdict_unchanged(self) -> None:
        """An already-qualified ``owner/repo`` clear keys the verdict identically — no behaviour change."""
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        result = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "clear",
                "860",
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
        verdict = ReviewVerdict.objects.get(pk=result["recorded_verdict_id"])
        assert verdict.slug == "souliane/teatree"
        assert resolve_pr_repo_slug(clear) == "souliane/teatree"
        assert_review_verdict_gate(slug=verdict.slug, pr_id=clear.pr_id, head_sha=clear.reviewed_sha)

    def test_whitespace_padded_slug_keys_verdict_where_merge_gate_resolves(self) -> None:
        """Whitespace must not flip ``_looks_like_owner_repo`` and split the verdict key.

        Record-time resolution keys off the request slug; the merge gate keys off
        the persisted ``clear.slug`` (which ``issue()`` stores stripped). A padded
        branch-name slug (``  fix/clear-slug  ``) momentarily passes the
        ``owner/repo`` structural check while padded, so record-time would key the
        verdict under the raw branch name — where the gate, resolving the stripped
        slug to the ticket's repo, never queries. Stripping at request construction
        keeps both sides on the identical normalized ``souliane/teatree``.
        """
        ticket = Ticket.objects.create(
            overlay="t3-teatree",
            state=Ticket.State.IN_REVIEW,
            issue_url="https://github.com/souliane/teatree/issues/859",
        )
        result = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "clear",
                "859",
                "  fix/clear-slug  ",
                reviewed_sha=_SHA,
                reviewer_identity="cold-reviewer",
                gh_verify_result="green",
                blast_class="docs",
                ticket_id=int(ticket.pk),
            ),
        )
        assert result["issued"]
        clear = MergeClear.objects.get(pk=result["clear_id"])
        assert clear.slug == "fix/clear-slug"

        resolved = resolve_pr_repo_slug(clear)
        assert resolved == "souliane/teatree"

        verdict = ReviewVerdict.objects.get(pk=result["recorded_verdict_id"])
        assert verdict.slug == resolved
        assert_review_verdict_gate(slug=resolved, pr_id=clear.pr_id, head_sha=clear.reviewed_sha)

    def test_whitespace_padded_qualified_slug_records_verdict_unchanged(self) -> None:
        """An already-qualified slug with surrounding whitespace records identically — no behaviour change."""
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        result = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "clear",
                "860",
                "  souliane/teatree  ",
                reviewed_sha=_SHA,
                reviewer_identity="cold-reviewer",
                gh_verify_result="green",
                blast_class="docs",
                ticket_id=int(ticket.pk),
            ),
        )
        assert result["issued"]
        clear = MergeClear.objects.get(pk=result["clear_id"])
        assert clear.slug == "souliane/teatree"
        verdict = ReviewVerdict.objects.get(pk=result["recorded_verdict_id"])
        assert verdict.slug == "souliane/teatree"
        assert resolve_pr_repo_slug(clear) == "souliane/teatree"
        assert_review_verdict_gate(slug=verdict.slug, pr_id=clear.pr_id, head_sha=clear.reviewed_sha)


class TestClearResolvesVerdictSlugBeforeIssuing(TestCase):
    """The verdict owner/repo is resolved BEFORE issuing — a resolution failure never orphans a CLEAR.

    ``resolve_pr_repo_slug`` raises ``MergePreconditionError`` in a degenerate
    environment (a workstream slug + no ticket ``issue_url`` + no resolvable clone
    ``origin``). Resolving it AFTER ``MergeClear.issue()`` persisted the row left an
    already-issued CLEAR orphaned behind a traceback; resolving up-front fails
    cleanly with nothing persisted, while the happy path is byte-identical.
    """

    def test_unresolvable_slug_refuses_before_issuing_and_persists_nothing(self) -> None:
        """No owner/repo resolvable → clean refusal, NO orphaned CLEAR, NO verdict row."""
        with patch("teatree.core.merge.pr_slug_resolution._project_repo_slug", return_value=""):
            result = cast(
                "dict[str, object]",
                call_command(
                    "ticket",
                    "clear",
                    "159",
                    "merge-candidate-working-repos",
                    reviewed_sha=_SHA,
                    reviewer_identity="cold-reviewer",
                    gh_verify_result="green",
                    blast_class="logic",
                ),
            )
        assert not result["issued"]
        error = cast("str", result["error"])
        assert "could not resolve" in error.lower()
        assert MergeClear.objects.count() == 0
        assert ReviewVerdict.objects.count() == 0

    def test_clone_origin_fallback_still_issues_and_records_verdict(self) -> None:
        """The happy twin: a resolvable clone origin issues the CLEAR and records the verdict under it."""
        with patch(
            "teatree.core.merge.pr_slug_resolution._project_repo_slug",
            return_value="souliane/teatree",
        ):
            result = cast(
                "dict[str, object]",
                call_command(
                    "ticket",
                    "clear",
                    "160",
                    "merge-candidate-working-repos",
                    reviewed_sha=_SHA,
                    reviewer_identity="cold-reviewer",
                    gh_verify_result="green",
                    blast_class="logic",
                ),
            )
        assert result["issued"]
        clear = MergeClear.objects.get(pk=result["clear_id"])
        verdict = ReviewVerdict.objects.get(pk=result["recorded_verdict_id"])
        assert verdict.slug == "souliane/teatree"
        assert resolve_pr_repo_slug(clear) == "souliane/teatree"
        assert_review_verdict_gate(slug=verdict.slug, pr_id=clear.pr_id, head_sha=clear.reviewed_sha)
