import contextlib
import io
import shlex
import sys
from dataclasses import replace
from typing import cast
from unittest.mock import MagicMock, patch

import click
import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.management.commands import _void_merge_commands
from teatree.core.management.commands import pr as pr_mod
from teatree.core.management.commands._void_merge_commands import VoidMergeResult
from teatree.core.management.refusal_exit import REFUSAL_EXIT_CODE
from teatree.core.merge.void_merge import VoidRefusal, VoidRefusedError
from teatree.core.models import MergeVoid, PullRequest, Ticket
from teatree.core.models.transition import TicketTransition
from teatree.hooks.void_merge_detect import DENY_REASON, void_merge_deny_reason
from tests.void_merge_fixtures import ABSENT, ISSUE_URL, PR_URL, Terminal, human_operator, landed_ticket, stub_forge

State = Ticket.State


def _rows() -> tuple[int, int, list[str], list[str]]:
    return (
        MergeVoid.objects.count(),
        TicketTransition.objects.count(),
        list(Ticket.objects.order_by("pk").values_list("state", flat=True)),
        list(PullRequest.objects.order_by("pk").values_list("state", flat=True)),
    )


def _run(*args: str, stdin: Terminal | None = None, stdout: Terminal | None = None) -> tuple[VoidMergeResult, str]:
    stdout = stdout or Terminal(tty=False)
    with (
        patch.object(sys, "stdin", stdin or Terminal(tty=False)),
        patch.object(sys, "stdout", stdout),
        patch.object(sys, "stderr", io.StringIO()),
    ):
        result = cast("VoidMergeResult", call_command("pr", "void-merge", PR_URL, *args))
    return result, stdout.getvalue()


class TestVoidMergeCommand(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(human_operator())
        self.ticket = landed_ticket()

    def test_the_command_takes_a_pr_url_and_only_reason_by_and_dry_run(self) -> None:
        params = pr_mod.Command().get_subcommand("void-merge").click_command.params

        assert {param.name for param in params} == {"pr_url", "reason", "by", "dry_run"}
        assert next(param for param in params if param.name == "dry_run").opts == ["--dry-run"]

    def test_a_dry_run_prints_the_evidence_and_writes_nothing_without_a_terminal(self) -> None:
        before = _rows()

        with stub_forge(ABSENT):
            result, out = _run("--dry-run")

        assert _rows() == before
        assert "error" not in result
        assert (result["verdict"], result["changed_paths"], result["absent_paths"]) == ("absent", 2, 2)
        assert f"ticket {self.ticket.pk}: merged -> coded" in out
        assert "2 of 2 paths absent" in out

    def test_a_dry_run_of_a_refusing_verdict_returns_the_refusal(self) -> None:
        carried = replace(ABSENT, merge_blobs=ABSENT.head_blobs)

        with stub_forge(carried):
            result, out = _run("--dry-run")

        assert (result["error"].split(":")[0], result["absent_paths"]) == ("content_carried_by_merge", 0)
        assert "verdict carried_by_merge: 0 of 2 paths absent" in out
        assert "REFUSED content_carried_by_merge" in out

    def test_operator_refusals_come_before_any_forge_read(self) -> None:
        for args, code in (
            (("--by", "alice"), "reason_missing"),
            (("--reason", "history rewrite", "--by", "coding-agent"), "actor_not_human"),
            (("--reason", "history rewrite"), "actor_not_human"),
        ):
            with self.subTest(args=args), stub_forge() as probe:
                result, _ = _run(*args)
            assert result["error"].startswith(code)
            probe.assert_not_called()

    def test_a_caller_without_a_terminal_writes_nothing(self) -> None:
        before = _rows()

        with stub_forge():
            result, _ = _run("--reason", "history rewrite", "--by", "alice")

        assert result["error"].startswith("no_terminal")
        assert _rows() == before

    def test_a_refusal_exits_non_zero_from_the_shell(self) -> None:
        argv = ["manage.py", "pr", "void-merge", PR_URL, "--reason", "history rewrite", "--by", "alice"]
        for extra in ([], ["--dry-run"]):
            present = replace(ABSENT, merge_blobs=ABSENT.head_blobs)
            with self.subTest(extra=extra), stub_forge(present, present), pytest.raises(SystemExit) as exit_:
                pr_mod.Command().run_from_argv([*argv, *extra])
            assert exit_.value.code == REFUSAL_EXIT_CODE

    def test_a_human_at_a_terminal_voids_and_the_command_re_reads_what_it_wrote(self) -> None:
        with stub_forge():
            result, out = _run(
                "--reason", "history rewrite", "--by", "alice", stdin=Terminal("yes\n"), stdout=Terminal()
            )

        void = MergeVoid.objects.get()
        assert result["void_id"] == void.pk
        assert out.index("2 of 2 paths absent") < out.index("Type 'yes' to approve") < out.index("merge voided:")
        assert f"merge voided: {PR_URL} @{void.merged_sha[:8]} by alice at " in out
        assert f"ticket {self.ticket.pk}: coded" in out
        assert f"pull request row {PullRequest.objects.get().pk}: closed" in out
        assert f"reopen {ISSUE_URL} before re-delivery if it is closed" in out

    def test_a_ticket_keyed_on_the_voided_pr_gets_no_reopen_reminder(self) -> None:
        keyed = landed_ticket(issue_url=PR_URL, with_row=False)

        with stub_forge():
            _, out = _run("--reason", "history rewrite", "--by", "alice", stdin=Terminal("yes\n"), stdout=Terminal())

        assert f"ticket {keyed.pk}: coded" in out
        assert out.count("reopen ") == 1


class TestTheAgentHookReadsTheArgvAsTheParserDoes(TestCase):
    params = pr_mod.Command().get_subcommand("void-merge").click_command.params

    def _parse(self, tail: list[str]) -> tuple[MagicMock, MagicMock, str]:
        """The real parser on *tail*: the write path's operator check, the forge probe, and stdout."""
        stop = VoidRefusedError(VoidRefusal.REASON_MISSING, "parsed as a write")
        out = Terminal(tty=False)
        with (
            patch.object(_void_merge_commands, "check_operator", side_effect=stop) as write_path,
            stub_forge(ABSENT) as probe,
            patch.object(sys, "stdout", out),
            patch.object(sys, "stderr", io.StringIO()),
            contextlib.suppress(SystemExit),
        ):
            pr_mod.Command().run_from_argv(["manage.py", "pr", "void-merge", PR_URL, *tail])
        return write_path, probe, out.getvalue()

    def test_every_value_option_takes_a_read_only_flag_as_its_value_and_the_hook_refuses_it(self) -> None:
        value_options = [o for p in self.params if isinstance(p, click.Option) and not p.is_flag for o in p.opts]
        assert value_options

        for option in value_options:
            for flag in ("--dry-run", "--help"):
                with self.subTest(option=option, flag=flag):
                    write_path, _, _ = self._parse([option, flag])
                    assert write_path.called
                    assert void_merge_deny_reason(f"t3 teatree pr void-merge {PR_URL} {option} {flag}") == DENY_REASON

    def test_every_form_the_hook_lets_an_agent_run_parses_as_a_read(self) -> None:
        assert next(p for p in self.params if p.name == "dry_run").secondary_opts == []

        for tail in (
            "--dry-run",
            "--dry-run --reason x",
            "--reason 'a b' --by alice --dry-run",
            "--reason=--by --dry-run",
        ):
            with self.subTest(tail=tail):
                assert void_merge_deny_reason(f"t3 teatree pr void-merge {PR_URL} {tail}") is None
                write_path, probe, _ = self._parse(shlex.split(tail))
                assert (write_path.called, probe.called) == (False, True)

        assert void_merge_deny_reason(f"t3 teatree pr void-merge {PR_URL} --help") is None
        write_path, probe, out = self._parse(["--help"])
        assert (write_path.called, probe.called) == (False, False)
        assert "Usage:" in out
