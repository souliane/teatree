"""`t3 <overlay> ticket set-target-branch` — the stacked-delivery override setter.

Writes the repo-keyed ``Ticket.extra['target_branch']`` map consumed by
``resolve_pr_target_branch`` and the merge-prediction gates through the locked
``merge_extra`` seam.
"""

import json
from io import StringIO
from typing import cast

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import Ticket


class TicketSetTargetBranchTest(TestCase):
    def test_records_bare_target_branch_for_the_named_repo(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/1")
        result = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "set-target-branch",
                str(ticket.pk),
                "acme/repo-a",
                "origin/stack-layer-1",
            ),
        )
        ticket.refresh_from_db()
        assert ticket.extra["target_branch"] == {"acme/repo-a": "stack-layer-1"}
        assert result == {
            "ticket_id": int(ticket.pk),
            "repo_slug": "acme/repo-a",
            "target_branch": "stack-layer-1",
        }

    def test_preserves_a_sibling_repos_override(self) -> None:
        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://example.com/issues/2",
            extra={"target_branch": {"acme/repo-a": "stack-a"}},
        )

        call_command("ticket", "set-target-branch", str(ticket.pk), "acme/repo-b", "stack-b")

        ticket.refresh_from_db()
        assert ticket.extra["target_branch"] == {
            "acme/repo-a": "stack-a",
            "acme/repo-b": "stack-b",
        }

    def test_migrates_a_legacy_scalar_to_the_scoped_mapping(self) -> None:
        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://example.com/issues/legacy",
            extra={"target_branch": "legacy-stack", "evidence": "preserved"},
        )

        call_command("ticket", "set-target-branch", str(ticket.pk), "acme/repo-a", "new-stack")

        ticket.refresh_from_db()
        assert ticket.extra == {
            "target_branch": {"acme/repo-a": "new-stack"},
            "evidence": "preserved",
        }

    def test_json_output_is_one_machine_document(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/3")
        output = StringIO()
        result = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "set-target-branch",
                str(ticket.pk),
                "https://github.com/acme/repo-b.git",
                "stack-layer-2",
                "--json",
                stdout=output,
            ),
        )
        assert json.loads(output.getvalue()) == result

    def test_blank_branch_exits_nonzero_and_records_nothing(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/4")
        with pytest.raises(SystemExit):
            call_command("ticket", "set-target-branch", str(ticket.pk), "acme/repo-a", "   ")
        ticket.refresh_from_db()
        assert "target_branch" not in (ticket.extra or {})

    def test_invalid_repo_exits_nonzero_and_records_nothing(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/5")
        with pytest.raises(SystemExit):
            call_command("ticket", "set-target-branch", str(ticket.pk), "repo-a", "stack-a")
        ticket.refresh_from_db()
        assert "target_branch" not in (ticket.extra or {})

    def test_unknown_ticket_exits_nonzero(self) -> None:
        with pytest.raises(SystemExit):
            call_command("ticket", "set-target-branch", "999999", "acme/repo-a", "main")
