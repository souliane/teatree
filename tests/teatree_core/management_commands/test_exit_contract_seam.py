"""Every returned refusal exits non-zero on the argv path (#4234).

#4234 enumerated 22 management-command sites that signal failure by RETURNING
it. Django's ``run_from_argv`` discards a command's return, so the process
exited 0 on a failure the command had correctly detected — and
``t3 <overlay> ship <id> && t3 <overlay> ticket clear …`` ran the second
command on a refused first, at the merge-authorisation seam.

The ``env`` group's six bare-``int`` sites, spread over five subcommands, raise.
The rest return a structured dict an in-process caller routes on —
``CallCommandMergeKeystone.merge_clear`` reads ``merged`` / ``merged_sha`` /
``error`` / ``escalation_kind`` / ``standing_delegation_by`` off ``ticket
merge`` — so raising there would destroy the value the loop reads. They inherit
:class:`~teatree.core.management.refusal_exit.RefusalExitTyperCommand`, which
restores the exit code at the argv boundary alone (#4210).

#4234's own enumeration was one site short: ``env migrate-secrets`` returned its
code from a conditional expression, which the constant-only scan behind that
count could not see. ``_returns_non_zero_int`` below descends into one.

Two guards, because either alone is weak: the AST ratchet proves no command
class *escapes* the seam, and the live cases prove the seam *fires*.
"""

import ast
import inspect
import io
import json
import textwrap
from collections.abc import Callable
from importlib import import_module
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command, get_commands
from django.test import TestCase

from teatree.core.gates.schema_guard import SelfDbMigrationError
from teatree.core.management.commands import e2e as e2e_mod
from teatree.core.management.commands import env as env_mod
from teatree.core.management.commands import followup as followup_mod
from teatree.core.management.commands import lifecycle as lifecycle_mod
from teatree.core.management.commands import repro as repro_mod
from teatree.core.management.commands import retro as retro_mod
from teatree.core.management.commands import review as review_mod
from teatree.core.management.commands import ticket as ticket_mod
from teatree.core.management.refusal_exit import REFUSAL_EXIT_CODE, RefusalExitTyperCommand
from teatree.core.models import Ticket, Worktree
from teatree.core.models.e2e_bypass import E2EBypassApproval, E2EBypassApprovalError
from teatree.core.models.repro_evidence import ReproEvidenceError
from teatree.core.models.repro_waiver import ReproWaiverError
from teatree.utils.postgres_secret import PostgresPasswordUnavailableError

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)

_A_SHA = "a" * 40

#: Command groups the ratchet must still find a structured refusal in. A scan
#: that stops matching would otherwise pass an empty set silently. `e2e`,
#: `lifecycle` and `repro` left the set when this fork converted their returned
#: refusals to `raise SystemExit` — a shrink is the ratchet working, so the
#: anti-vacuity floor moves with it rather than pinning groups now clean.
_GROUPS_WITH_STRUCTURED_REFUSALS = frozenset({"followup", "pr", "review", "ticket"})


def _is_command_decorator(decorator: ast.expr) -> bool:
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    if isinstance(target, ast.Name):
        return target.id in {"command", "initialize"}
    return isinstance(target, ast.Attribute) and target.attr in {"command", "initialize"}


def _returns_error_dict(value: ast.expr) -> bool:
    """True for ``return {… "error": … }`` — the shape the seam keys on."""
    if not isinstance(value, ast.Dict):
        return False
    return any(
        isinstance(key, ast.Constant) and isinstance(key.value, str) and "error" in key.value.lower()
        for key in value.keys
    )


def _returns_non_zero_int(value: ast.expr) -> bool:
    """True for ``return 1`` and for any operand of a ternary, sign or boolean chain.

    Each reads as an exit code and none is one. Every composite shape needs its
    own descent: matching ``ast.Constant`` alone is how ``env migrate-secrets``
    kept returning its code past this ratchet, and ``return -1`` /
    ``return failures and 1 or 0`` were the same blind spot in ``ast.UnaryOp`` /
    ``ast.BoolOp`` clothing. ``not`` is excluded from the sign descent because
    it yields a bool, which is never an exit code.
    """
    if isinstance(value, ast.IfExp):
        return _returns_non_zero_int(value.body) or _returns_non_zero_int(value.orelse)
    if isinstance(value, ast.BoolOp):
        return any(_returns_non_zero_int(operand) for operand in value.values)
    if isinstance(value, ast.UnaryOp) and isinstance(value.op, ast.USub | ast.UAdd):
        return _returns_non_zero_int(value.operand)
    if not (isinstance(value, ast.Constant) and isinstance(value.value, int)):
        return False
    return not isinstance(value.value, bool) and value.value != 0


def _offending_returns(klass: type, predicate: Callable[[ast.expr], bool]) -> list[str]:
    """``"<class>.<method>:<lineno>"`` for each ``@command`` return matching *predicate*."""
    try:
        source = textwrap.dedent(inspect.getsource(klass))
    except (OSError, TypeError):  # pragma: no cover — a C-implemented or REPL-defined base
        return []
    offences: list[str] = []
    for func in ast.walk(ast.parse(source)):
        if not isinstance(func, ast.FunctionDef) or not any(_is_command_decorator(d) for d in func.decorator_list):
            continue
        offences += [
            f"{klass.__name__}.{func.name}:{stmt.lineno}"
            for stmt in ast.walk(func)
            if isinstance(stmt, ast.Return) and stmt.value is not None and predicate(stmt.value)
        ]
    return offences


def _teatree_command_classes() -> dict[str, type]:
    """Every registered management command teatree itself ships, by CLI name.

    Imported directly rather than through ``load_command_class`` — that helper
    instantiates, and a helper module in the same package (``tasks_session_view``)
    deliberately exposes no ``Command``.
    """
    classes: dict[str, type] = {}
    for name, app in get_commands().items():
        if not (isinstance(app, str) and app.startswith("teatree")):
            continue
        module = import_module(f"{app}.management.commands.{name}")
        klass = getattr(module, "Command", None)
        if isinstance(klass, type) and klass.__module__.startswith("teatree"):
            classes[name] = klass
    return classes


def _offenders_across_mro(klass: type, predicate: Callable[[ast.expr], bool]) -> list[str]:
    return [offence for base in klass.__mro__ for offence in _offending_returns(base, predicate)]


class TestNoCommandReturnsABareNonZeroInt:
    """A returned int is discarded by every call path — the only fix is to raise."""

    def test_the_whole_command_tree_is_clean(self) -> None:
        violations = {
            name: offences
            for name, klass in _teatree_command_classes().items()
            if (offences := _offenders_across_mro(klass, _returns_non_zero_int))
        }
        assert violations == {}, (
            "Management command(s) returning a non-zero int — django-typer serialises it to stdout "
            f"and exits 0. Use `self.stderr.write(...)` then `raise SystemExit(N)`: {violations}"
        )

    def test_the_detector_flags_the_shape_it_claims_to(self) -> None:
        source = textwrap.dedent(
            """
            class Probe:
                @command()
                def sub(self):
                    return 2

                @command()
                def conditional(self):
                    return 0 if ok else 1

                @command()
                def negated(self):
                    return -1

                @command()
                def chained(self):
                    return failures and 1 or 0
            """,
        )
        tree = ast.parse(source)
        returns = [node for node in ast.walk(tree) if isinstance(node, ast.Return) and node.value is not None]
        assert [_returns_non_zero_int(node.value) for node in returns if node.value] == [True] * 4
        assert not _returns_non_zero_int(ast.Constant(value=0))
        assert not _returns_non_zero_int(ast.Constant(value=True))
        assert not _returns_non_zero_int(ast.parse("0 if ok else 0", mode="eval").body)
        assert not _returns_non_zero_int(ast.parse("-0", mode="eval").body)
        assert not _returns_non_zero_int(ast.parse("failures and 0 or 0", mode="eval").body)
        assert not _returns_non_zero_int(ast.parse("not 1", mode="eval").body)


class TestEveryStructuredRefusalCarriesTheSeam:
    """A ``{"error": …}`` return is sanctioned ONLY because the seam restores the exit code."""

    def test_no_command_class_escapes_the_seam(self) -> None:
        unseamed = {
            name: klass
            for name, klass in _teatree_command_classes().items()
            if not issubclass(klass, RefusalExitTyperCommand)
        }
        uncovered = {
            name: offences
            for name, klass in unseamed.items()
            if (offences := _offenders_across_mro(klass, _returns_error_dict))
        }
        assert uncovered == {}, (
            "Management command(s) returning a structured refusal without the exit-code seam — "
            "inherit `RefusalExitTyperCommand` (see /t3:internals § Structured refusals): "
            f"{uncovered}"
        )

    def test_the_ratchet_is_not_vacuous(self) -> None:
        """The scan still finds refusals — an empty scan would pass the guard above silently."""
        covered = {
            name
            for name, klass in _teatree_command_classes().items()
            if _offenders_across_mro(klass, _returns_error_dict)
        }
        assert covered >= _GROUPS_WITH_STRUCTURED_REFUSALS, (
            f"the detector stopped seeing structured refusals in {sorted(_GROUPS_WITH_STRUCTURED_REFUSALS - covered)}"
        )

    def test_only_the_pre_push_hook_entry_point_keeps_a_soft_refusal(self) -> None:
        """A soft exemption silences the seam — ``pr ensure-pr`` (#792) is the whole list."""
        exempt = {
            name: sorted(klass.soft_refusal_commands)
            for name, klass in _teatree_command_classes().items()
            if issubclass(klass, RefusalExitTyperCommand) and klass.soft_refusal_commands
        }
        assert exempt == {"pr": ["ensure-pr"]}


class TestTicketGroupRefusalsExitNonZero(TestCase):
    """The merge-authorisation seam: every ``ticket`` refusal fails the shell."""

    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/4234")

    @staticmethod
    def _argv(*args: str) -> None:
        ticket_mod.Command().run_from_argv(["manage.py", "ticket", *args])

    def test_transition_rejects_an_unknown_name(self) -> None:
        with pytest.raises(SystemExit) as exc:
            self._argv("transition", "1", "no-such-transition")
        assert exc.value.code == REFUSAL_EXIT_CODE

    def test_clear_rejects_a_missing_ticket(self) -> None:
        with pytest.raises(SystemExit) as exc:
            self._argv(
                "clear",
                "77",
                "owner/repo",
                "--reviewed-sha",
                _A_SHA,
                "--reviewer-identity",
                "cold-reviewer",
                "--ticket-id",
                "987654",
            )
        assert exc.value.code == REFUSAL_EXIT_CODE

    def test_merge_rejects_a_missing_clear(self) -> None:
        with pytest.raises(SystemExit) as exc:
            self._argv("merge", "987654")
        assert exc.value.code == REFUSAL_EXIT_CODE

    def test_comment_rejects_an_empty_body(self) -> None:
        with pytest.raises(SystemExit) as exc:
            self._argv("comment", "https://example.com/issues/1")
        assert exc.value.code == REFUSAL_EXIT_CODE

    def test_create_sub_rejects_a_missing_parent(self) -> None:
        with pytest.raises(SystemExit) as exc:
            self._argv("create-sub", "--title", "child")
        assert exc.value.code == REFUSAL_EXIT_CODE

    def test_rubric_set_rejects_absent_criteria(self) -> None:
        with pytest.raises(SystemExit) as exc:
            self._argv("rubric-set", str(self.ticket.pk))
        assert exc.value.code == REFUSAL_EXIT_CODE

    def test_e2e_bypass_rejects_a_refused_approval(self) -> None:
        with (
            patch.object(E2EBypassApproval, "record", side_effect=E2EBypassApprovalError("maker cannot self-approve")),
            pytest.raises(SystemExit) as exc,
        ):
            self._argv("e2e-bypass", str(self.ticket.pk), "--approver", "maker", "--head-sha", _A_SHA)
        assert exc.value.code == REFUSAL_EXIT_CODE

    def test_the_loop_still_reads_the_merge_dict_in_process(self) -> None:
        """The seam is argv-only: ``CallCommandMergeKeystone`` reads five keys off this."""
        result = call_command("ticket", "merge", "987654")

        assert isinstance(result, dict)
        assert result["merged"] is False
        assert "not found" in str(result["error"])


class TestReviewGroupRefusalsExitNonZero(TestCase):
    """``review`` records verdicts and evidence — a refusal must not read as recorded."""

    @staticmethod
    def _argv(*args: str) -> None:
        review_mod.Command().run_from_argv(["manage.py", "review", *args])

    def test_record_rejects_malformed_findings(self) -> None:
        with pytest.raises(SystemExit) as exc:
            self._argv(
                "record",
                "77",
                "owner/repo",
                "merge_safe",
                "--reviewed-sha",
                _A_SHA,
                "--reviewer-identity",
                "cold-reviewer",
                "--findings-json",
                "{not json",
            )
        assert exc.value.code == REFUSAL_EXIT_CODE

    def test_record_evidence_rejects_a_blank_verdict(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/4235")
        with pytest.raises(SystemExit) as exc:
            self._argv(
                "record-evidence",
                str(ticket.pk),
                "--kind",
                "cold_review",
                "--reviewer",
                "cold-reviewer",
                "--verdict",
                "",
                "--head-sha",
                _A_SHA,
            )
        assert exc.value.code == REFUSAL_EXIT_CODE

    def test_lock_acquire_rejects_a_stale_schema(self) -> None:
        with (
            patch.object(review_mod, "require_current_schema", side_effect=SelfDbMigrationError("migrate first")),
            pytest.raises(SystemExit) as exc,
        ):
            self._argv("lock-acquire", "https://example.com/owner/repo/-/merge_requests/7", "--holder", "dispatcher")
        assert exc.value.code == REFUSAL_EXIT_CODE


class TestReproGroupRefusalsExitNonZero(TestCase):
    """Forced-repro recording: an unrecorded RED/GREEN must not read as recorded."""

    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/4236")

    @staticmethod
    def _argv(*args: str) -> None:
        repro_mod.Command().run_from_argv(["manage.py", "repro", *args])

    def _assert_refused_recording_exits(self, verb: str, factory: str) -> None:
        with (
            patch.object(repro_mod, "_run_repro", return_value=MagicMock(exit_code=1)),
            patch.object(repro_mod, "_resolve_cwd", return_value="/tmp"),
            patch.object(repro_mod.git, "head_sha", return_value=_A_SHA),
            patch.object(repro_mod.ReproEvidence, factory, side_effect=ReproEvidenceError("no provenance")),
            pytest.raises(SystemExit) as exc,
        ):
            self._argv(verb, str(self.ticket.pk), "--command", "pytest -q")
        assert exc.value.code == REFUSAL_EXIT_CODE

    def test_record_red_rejects_a_refused_recording(self) -> None:
        self._assert_refused_recording_exits("record-red", "record_red")

    def test_record_green_rejects_a_refused_recording(self) -> None:
        self._assert_refused_recording_exits("record-green", "record_green")

    def test_waive_rejects_a_refused_waiver(self) -> None:
        with (
            patch.object(repro_mod.ReproWaiver, "record", side_effect=ReproWaiverError("maker cannot self-waive")),
            pytest.raises(SystemExit) as exc,
        ):
            self._argv("waive", str(self.ticket.pk), "--approver", "maker", "--reason", "flaky")
        assert exc.value.code == REFUSAL_EXIT_CODE


class TestLifecycleGroupRefusalsExitNonZero(TestCase):
    """``lifecycle record-e2e-run`` gates the merge — an unrecorded run must fail loud."""

    def test_record_e2e_run_rejects_a_blank_spec(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/4237")
        with pytest.raises(SystemExit) as exc:
            lifecycle_mod.Command().run_from_argv(
                ["manage.py", "lifecycle", "record-e2e-run", str(ticket.pk), "--spec", "", "--head-sha", _A_SHA],
            )
        assert exc.value.code == REFUSAL_EXIT_CODE


class TestEnvGroupRefusalsExitNonZero(TestCase):
    """A failed ``migrate-secrets`` leaves the literal in the cache — the shell must see that."""

    def test_migrate_secrets_rejects_a_worktree_it_could_not_migrate(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/4238")
        worktree = Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="backend",
            branch="ac-test",
            extra={"worktree_path": "/tmp/wt/backend"},
        )
        with (
            patch.object(env_mod, "resolve_worktree", return_value=worktree),
            patch.object(env_mod, "env_cache_path", return_value=Path("/tmp/wt/.t3-cache/backend/.t3-env.cache")),
            patch.object(env_mod, "extract_literal_from_cache", return_value="<unmigrated>"),
            patch.object(
                env_mod,
                "ensure_postgres_pass_entry",
                side_effect=PostgresPasswordUnavailableError("no pass installed"),
            ),
            pytest.raises(SystemExit) as exc,
        ):
            env_mod.Command().run_from_argv(
                ["manage.py", "env", "migrate-secrets", "--path", "/tmp/wt/backend"],
            )
        assert exc.value.code == 1


class TestOverlayBackedRefusalsExitNonZero:
    def test_e2e_trigger_ci_rejects_a_missing_config(self) -> None:
        overlay = MagicMock()
        overlay.metadata.get_e2e_config.return_value = {}
        with (
            patch.object(e2e_mod, "get_overlay", return_value=overlay),
            pytest.raises(SystemExit) as exc,
        ):
            e2e_mod.Command().run_from_argv(["manage.py", "e2e", "trigger-ci"])
        assert exc.value.code == REFUSAL_EXIT_CODE

    def test_followup_discover_mrs_rejects_a_missing_code_host(self) -> None:
        with (
            patch.object(followup_mod, "code_host_from_overlay", return_value=None),
            pytest.raises(SystemExit) as exc,
        ):
            followup_mod.Command().run_from_argv(["manage.py", "followup", "discover-mrs"])
        assert exc.value.code == REFUSAL_EXIT_CODE


class TestRetroReviewFindingsExitsNonZero(TestCase):
    """``retro review-findings`` wraps its refusal in ``json.dumps`` before returning.

    Neither the runtime seam (a ``str``, not a ``Mapping``) nor the AST ratchet
    (a ``Call``, not a literal ``{"error": …}``, at the ``@command``-decorated
    method's own ``return``) can see this shape — a cold review of #4235 found
    it escaping both guards. Fixed locally in ``review_findings`` by routing
    the same ``refusal_exit_code`` predicate the seam uses; pinned here rather
    than by widening the shared ratchet, since a broader helper-method walk
    also flags ``handover.py``'s list-nested ``error`` key and ``tasks.py``'s
    ``routing_error`` substring match — both already-accepted, non-blocking
    asymmetries a widened ratchet would wrongly turn into new failures.

    The exit code alone is not the contract. The seam's siblings print the
    refusal from ``super().execute()`` and only then raise; a raise from inside
    the command method lands before any write, so a first cut of this fix exited
    1 with both streams empty — a failure carrying no reason, and no ``error``
    payload for a machine consumer. Every case below asserts the payload too.
    """

    @staticmethod
    def _argv(out: io.StringIO, *args: str) -> None:
        retro_mod.Command(stdout=out).run_from_argv(["manage.py", "retro", *args])

    def test_review_findings_rejects_an_unrecognised_pr_url(self) -> None:
        out = io.StringIO()
        with pytest.raises(SystemExit) as exc:
            self._argv(out, "review-findings", "not-a-recognised-url")
        assert exc.value.code == REFUSAL_EXIT_CODE
        assert "not a recognised" in str(json.loads(out.getvalue())["error"]).lower()

    def test_review_findings_rejects_a_missing_classification_file(self) -> None:
        """The ``_file_findings`` refusals reach the stream through the same call site."""
        host = MagicMock()
        host.list_pr_comments.return_value = []
        out = io.StringIO()
        with (
            patch.object(retro_mod.Command, "_resolve_host", staticmethod(lambda _url: host)),
            pytest.raises(SystemExit) as exc,
        ):
            self._argv(
                out,
                "review-findings",
                "https://github.com/o/r/pull/1",
                "--classification",
                "/nonexistent/verdicts.json",
            )
        assert exc.value.code == REFUSAL_EXIT_CODE
        assert "classification file not found" in str(json.loads(out.getvalue())["error"]).lower()

    def test_call_command_still_reads_the_json_string_in_process(self) -> None:
        """The seam is argv-only: ``call_command`` callers still get the JSON string, not a raise."""
        result = call_command("retro", "review-findings", "not-a-recognised-url")

        assert isinstance(result, str)
        parsed = json.loads(result)
        assert "not a recognised" in str(parsed["error"]).lower()
