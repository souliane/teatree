"""Recorded per-invocation user-approval channel for the #777 gate (#953).

The #777 interactive-approval gate hard-rejected every non-TTY caller, so a
chat-only operator + any agent could never run ``db refresh --fresh-dump``.
``DbApproval`` adds a recorded, single-use, op+tenant-scoped user approval
that satisfies the *same* gate without removing the interactive-TTY path —
mirroring the ``MergeClear``/``MergeAudit`` safety model 1:1.

These exercise the real ``DbApproval``/``DbAudit`` models and the real ``db
refresh`` command end to end through ``call_command``; only the unstoppable
external — the stdin/stdout TTY state — is faked, exactly the carve-out the
Test-Writing Doctrine reserves. The decisive new-capability case (iii) fails
on the pre-#953 code (no recorded channel exists) and passes after.
"""

import io
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase, override_settings

from teatree.core.models import DbApproval, DbApprovalError, DbAudit, Ticket, Worktree
from tests.teatree_core.management_commands._overlays import FULL_OVERLAY, SETTINGS, _patch_overlays

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)

# FullOverlay.get_db_import_strategy → source_database == "test_db";
# the db refresh fresh-dump op is scoped to op="fresh-dump", tenant="test_db".
_OP = "fresh-dump"
_TENANT = "test_db"
_USER = "souliane"


class _FakeStream(io.StringIO):
    def __init__(self, *, tty: bool, content: str = "") -> None:
        super().__init__(content)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _make_worktree(wt_dir: Path) -> Worktree:
    ticket = Ticket.objects.create(overlay="test")
    worktree = Worktree.objects.create(
        overlay="test",
        ticket=ticket,
        repo_path="/tmp/test",
        branch="feature",
        extra={"worktree_path": str(wt_dir)},
    )
    worktree.provision()
    worktree.save()
    return worktree


def _run_fresh_dump(wt_dir: Path, *, user_authorized: str, tty: bool) -> object:
    """Invoke ``db refresh --fresh-dump`` with the TTY boundary faked only."""
    # A human at a TTY answers "yes"; the non-TTY case never reads stdin
    # (it is hard-refused before the prompt) but still carries a buffer so
    # an accidental read would not hang.
    stdin = _FakeStream(tty=tty, content="yes\n")
    stdout = _FakeStream(tty=tty)
    with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
        return call_command(
            "db",
            "refresh",
            path=str(wt_dir),
            fresh_dump=True,
            user_authorized=user_authorized,
        )


class TestDbApprovalRecordGuard(TestCase):
    """The guarded factory (≈ ``MergeClear.issue``) — never self-authorize."""

    def test_ii_self_issued_agent_role_approver_refused(self) -> None:
        """Case (ii): a maker/coding-agent/loop approver is refused at record."""
        for agent_id in ("loop", "coding-agent", "maker:cold", "loop-merge"):
            with pytest.raises(DbApprovalError, match="maker/coding-agent/loop"):
                DbApproval.record(_OP, _TENANT, agent_id)
        assert not DbApproval.objects.exists()  # rejected ⇒ no partial row

    def test_record_requires_non_empty_scope_and_approver(self) -> None:
        with pytest.raises(DbApprovalError, match="op is required"):
            DbApproval.record("", _TENANT, _USER)
        with pytest.raises(DbApprovalError, match="tenant is required"):
            DbApproval.record(_OP, "  ", _USER)
        with pytest.raises(DbApprovalError, match="approver_id is required"):
            DbApproval.record(_OP, _TENANT, "")

    def test_matches_is_exact_scoped_and_single_use(self) -> None:
        """Pure scope logic: matches only the exact op+tenant while unconsumed."""
        approval = DbApproval.record(_OP, _TENANT, _USER)
        assert approval.matches(_OP, _TENANT) is True
        assert approval.matches(_OP, "other-tenant") is False
        assert approval.matches("other-op", _TENANT) is False
        approval.consumed_at = approval.created_at
        assert approval.matches(_OP, _TENANT) is False  # single-use

    def test_str_renders_op_tenant_and_approver(self) -> None:
        approval = DbApproval.record(_OP, _TENANT, _USER)
        audit = DbAudit.objects.create(approval=approval, op=_OP, tenant=_TENANT, approver_id=_USER)
        assert _OP in str(approval)
        assert _USER in str(approval)
        assert _OP in str(audit)
        assert _USER in str(audit)


class TestDbRefreshRecordedChannel(TestCase):
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_i_no_approval_present_non_tty_refused(self) -> None:
        """Case (i): no recorded approval + no TTY ⇒ op REFUSED (SystemExit 1)."""
        with tempfile.TemporaryDirectory() as tmp:
            wt_dir = Path(tmp) / "test"
            wt_dir.mkdir()
            _make_worktree(wt_dir)
            assert not DbApproval.objects.exists()
            with pytest.raises(SystemExit) as exc:
                _run_fresh_dump(wt_dir, user_authorized="", tty=False)
            assert exc.value.code == 1

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_ii_agent_role_approval_cannot_be_recorded_so_op_refused(self) -> None:
        """Case (ii): the agent cannot record its own approval ⇒ still refused.

        ``DbApproval.record`` refuses an agent-role id, so no row exists for
        the agent to consume; the non-TTY op is refused.
        """
        with tempfile.TemporaryDirectory() as tmp:
            wt_dir = Path(tmp) / "test"
            wt_dir.mkdir()
            _make_worktree(wt_dir)
            with pytest.raises(DbApprovalError):
                DbApproval.record(_OP, _TENANT, "loop")
            with pytest.raises(SystemExit) as exc:
                _run_fresh_dump(wt_dir, user_authorized="loop", tty=False)
            assert exc.value.code == 1

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_iii_recorded_user_approval_lets_non_tty_caller_execute(self) -> None:
        """Case (iii): the new capability — recorded user approval, non-TTY OK.

        RED on pre-#953 code (no recorded channel exists; the non-TTY caller
        is hard-refused). GREEN after: the recorded single-use user
        ``DbApproval`` satisfies the gate and the op runs to completion.
        """
        with tempfile.TemporaryDirectory() as tmp:
            wt_dir = Path(tmp) / "test"
            wt_dir.mkdir()
            worktree = _make_worktree(wt_dir)
            DbApproval.record(_OP, _TENANT, _USER)

            result = _run_fresh_dump(wt_dir, user_authorized=_USER, tty=False)

            assert "refreshed" in str(result).lower()
            worktree.refresh_from_db()
            assert worktree.state == Worktree.State.PROVISIONED
            # single-use: the approval is consumed and cannot be reused
            approval = DbApproval.objects.get()
            assert approval.consumed_at is not None
            assert not approval.matches(_OP, _TENANT)

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_iv_audit_record_is_written(self) -> None:
        """Case (iv): consuming a recorded approval writes a ``DbAudit`` row."""
        with tempfile.TemporaryDirectory() as tmp:
            wt_dir = Path(tmp) / "test"
            wt_dir.mkdir()
            _make_worktree(wt_dir)
            DbApproval.record(_OP, _TENANT, _USER)

            _run_fresh_dump(wt_dir, user_authorized=_USER, tty=False)

            audit = DbAudit.objects.get()
            assert audit.op == _OP
            assert audit.tenant == _TENANT
            assert audit.approver_id == _USER
            assert audit.executed_at is not None
            assert audit.approval_id == DbApproval.objects.get().pk

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_v_op_or_tenant_scope_mismatch_refused(self) -> None:
        """Case (v): an approval for a different op/tenant does NOT satisfy.

        A recorded approval for ``tenant-b``+``fresh-dump`` (wrong tenant)
        must not authorize the requested ``test_db`` fresh-dump — it is
        refused and the wrongly-scoped approval is left unconsumed.
        """
        with tempfile.TemporaryDirectory() as tmp:
            wt_dir = Path(tmp) / "test"
            wt_dir.mkdir()
            _make_worktree(wt_dir)
            wrong_tenant = DbApproval.record(_OP, "tenant-b", _USER)
            wrong_op = DbApproval.record("dslr-snapshot", _TENANT, _USER)

            with pytest.raises(SystemExit) as exc:
                _run_fresh_dump(wt_dir, user_authorized=_USER, tty=False)
            assert exc.value.code == 1

            wrong_tenant.refresh_from_db()
            wrong_op.refresh_from_db()
            assert wrong_tenant.consumed_at is None
            assert wrong_op.consumed_at is None
            assert not DbAudit.objects.exists()

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_vii_non_tty_no_approval_refusal_names_expected_scope(self) -> None:
        """#126 gap 4: the non-TTY dead-end must surface the expected op+tenant scope.

        Without a recorded approval and no TTY the gate refuses — but a
        chat-only operator is then stuck unless the refusal tells them
        exactly which ``DbApproval`` to record (mirrors
        ``OnBehalfPostBlockedError`` naming the satisfying invocation).
        The refusal message must name the op and tenant.
        """
        with tempfile.TemporaryDirectory() as tmp:
            wt_dir = Path(tmp) / "test"
            wt_dir.mkdir()
            _make_worktree(wt_dir)
            stdin = _FakeStream(tty=False, content="yes\n")
            stdout = _FakeStream(tty=False)
            stderr = io.StringIO()
            with patch("sys.stdin", stdin), patch("sys.stdout", stdout), pytest.raises(SystemExit):
                call_command("db", "refresh", path=str(wt_dir), fresh_dump=True, user_authorized="", stderr=stderr)
            # The command surfaces the refusal on stderr; the hint must name
            # the op+tenant scope so a chat-only operator knows what to record.
            surfaced = stderr.getvalue()
            assert _OP in surfaced, f"refusal must name the op scope: {surfaced!r}"
            assert _TENANT in surfaced, f"refusal must name the tenant scope: {surfaced!r}"


class TestDbApprovalScopeHint(TestCase):
    """The recorded-approval refusal surfaces the expected scope (#126 gap 4)."""

    def test_require_approval_non_tty_no_record_names_scope(self) -> None:
        """A non-TTY refusal with no recorded approval names op+tenant in the message."""
        from teatree.core.gates.db_approval_gate import ApprovalScope, require_approval  # noqa: PLC0415
        from teatree.utils.approval import ApprovalRefusedError  # noqa: PLC0415

        stdin = _FakeStream(tty=False, content="yes\n")
        stdout = _FakeStream(tty=False)
        with pytest.raises(ApprovalRefusedError) as exc:
            require_approval(
                "Pull fresh DEV dump?",
                ApprovalScope(op=_OP, tenant=_TENANT, user_authorized=""),
                stdin=stdin,
                stdout=stdout,
            )
        message = str(exc.value)
        assert _OP in message, f"refusal must name the op scope: {message!r}"
        assert _TENANT in message, f"refusal must name the tenant scope: {message!r}"
        assert "approve" in message.lower(), f"refusal must point at the recorded-approval remedy: {message!r}"

    def test_require_approval_non_tty_matching_record_proceeds(self) -> None:
        """A non-TTY caller WITH a matching recorded approval proceeds (no refusal)."""
        from teatree.core.gates.db_approval_gate import ApprovalScope, require_approval  # noqa: PLC0415

        DbApproval.record(_OP, _TENANT, _USER)
        stdin = _FakeStream(tty=False, content="")
        stdout = _FakeStream(tty=False)
        require_approval(
            "Pull fresh DEV dump?",
            ApprovalScope(op=_OP, tenant=_TENANT, user_authorized=_USER),
            stdin=stdin,
            stdout=stdout,
        )
        assert DbApproval.objects.get().consumed_at is not None


class TestDbApprovalScopeNormalization(TestCase):
    """op/tenant are normalized identically at record and consume (#126 gap 4).

    Same drift class as the on-behalf target: a recorded approval whose op
    differs only by case/whitespace from the consume token must still match,
    while a genuinely different op/tenant must NOT.
    """

    def test_op_case_and_whitespace_normalized_at_both_ends(self) -> None:
        DbApproval.record("  Fresh-Dump  ", _TENANT, _USER)
        consumed = DbApproval.consume("fresh-dump", _TENANT)
        assert consumed is not None, "a case/whitespace-variant op must still match"

    def test_tenant_whitespace_normalized(self) -> None:
        DbApproval.record(_OP, "  test_db  ", _USER)
        consumed = DbApproval.consume(_OP, "test_db")
        assert consumed is not None

    def test_distinct_op_still_does_not_match(self) -> None:
        DbApproval.record(_OP, _TENANT, _USER)
        assert DbApproval.consume("dslr-snapshot", _TENANT) is None

    def test_distinct_tenant_still_does_not_match(self) -> None:
        DbApproval.record(_OP, _TENANT, _USER)
        assert DbApproval.consume(_OP, "tenant-b") is None

    def test_single_use_preserved_across_normalized_forms(self) -> None:
        DbApproval.record("FRESH-DUMP", _TENANT, _USER)
        assert DbApproval.consume("fresh-dump", _TENANT) is not None
        assert DbApproval.consume("Fresh-Dump", _TENANT) is None


class TestDbRefreshRecordedChannelTtyRegression(TestCase):
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_vi_interactive_tty_path_still_works(self) -> None:
        """Case (vi): regression — a human at a TTY answering ``y`` still works.

        No recorded approval and no --user-authorized: the unchanged
        interactive-TTY channel is used and a ``yes`` proceeds.
        """
        with tempfile.TemporaryDirectory() as tmp:
            wt_dir = Path(tmp) / "test"
            wt_dir.mkdir()
            worktree = _make_worktree(wt_dir)

            result = _run_fresh_dump(wt_dir, user_authorized="", tty=True)

            assert "refreshed" in str(result).lower()
            worktree.refresh_from_db()
            assert worktree.state == Worktree.State.PROVISIONED
            assert not DbApproval.objects.exists()
            assert not DbAudit.objects.exists()
