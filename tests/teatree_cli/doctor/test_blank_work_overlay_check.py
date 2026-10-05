"""Doctor reports unattributed work rows left by migration 0125."""

import io
from contextlib import redirect_stdout

from django.test import TestCase

from teatree.cli.doctor.checks_blank_work_overlays import check_blank_work_overlays
from teatree.core.models import ConfigSetting, Session, Ticket, Worktree


class TestBlankWorkOverlayCheck(TestCase):
    def test_blank_rows_report_counts_and_remedy(self) -> None:
        ticket = Ticket.objects.create(overlay="")
        Session.objects.create(ticket=ticket, overlay="")
        Worktree.objects.create(ticket=ticket, overlay="", repo_path="acme/widget", branch="feature")
        output = io.StringIO()

        with redirect_stdout(output):
            passed = check_blank_work_overlays()

        assert passed is False
        for model in ("Ticket", "Session", "Worktree"):
            assert f"1 blank-overlay {model} row(s)" in output.getvalue()
        assert "set the overlay on all affected rows" in output.getvalue()

    def _finding(self) -> tuple[bool, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            passed = check_blank_work_overlays()
        return passed, output.getvalue()

    def test_malformed_session_phase_is_reported(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree")
        session = Session.objects.create(ticket=ticket, overlay="t3-teatree")
        Session.objects.filter(pk=session.pk).update(visited_phases={"coding": True})

        passed, output = self._finding()

        assert passed is False
        assert "1 unresolved session phase row(s)" in output
        assert "Replace malformed visited_phases" in output

    def test_unresolved_pr_branch_is_reported(self) -> None:
        Ticket.objects.create(overlay="t3-teatree", extra={"pr_urls": ["https://example.com/a/b/pull/1"]})

        passed, output = self._finding()

        assert passed is False
        assert "1 unresolved PR branch row(s)" in output
        assert "Repair pr_urls and pr_url_by_branch" in output

    def test_unresolved_dream_ticket_is_reported(self) -> None:
        Ticket.objects.create(overlay="t3-teatree", extra={"dream_gap_key": "gap-1"})

        passed, output = self._finding()

        assert passed is False
        assert "1 unresolved dream ticket row(s)" in output
        assert "convert it into dream_gap_batch" in output

    def test_scope_collision_is_reported(self) -> None:
        ConfigSetting.objects.create(scope="teatree", key="wip", value="slow")
        ConfigSetting.objects.create(scope="t3-teatree", key="wip", value="full")

        passed, output = self._finding()

        assert passed is False
        assert "1 unresolved setting scope collision row(s)" in output
        assert "Merge each short-scope value" in output

    def test_non_list_global_private_repos_is_reported(self) -> None:
        ConfigSetting.objects.create(scope="", key="private_repos", value={"bad": "shape"})

        passed, output = self._finding()

        assert passed is False
        assert "1 non-list global private_repos row(s)" in output
        assert "Replace the global private_repos value" in output

    def test_invalid_and_blocked_legacy_private_repos_are_reported(self) -> None:
        ConfigSetting.objects.create(scope="", key="private_repos", value="bad")
        ConfigSetting.objects.create(scope="alpha", key="internal_publish_namespaces", value=["bad/repo"])

        passed, output = self._finding()

        assert passed is False
        assert "1 invalid legacy private namespace entry/row(s)" in output
        assert "1 legacy private namespace row(s) left uncarried" in output
        assert "carry every retained namespace entry" in output

    def test_a_legacy_namespace_row_still_counts_once_the_global_row_is_repaired(self) -> None:
        # Repairing the global row does not carry what 0124 had to leave behind; only deleting the row does.
        ConfigSetting.objects.create(scope="", key="private_repos", value=["github.com/acme/public-mirror"])
        ConfigSetting.objects.create(
            scope="alpha", key="internal_publish_namespaces", value=["gitlab.example.test/acme/widget"]
        )

        passed, output = self._finding()

        assert passed is False
        assert "1 legacy private namespace row(s) left uncarried" in output

    def test_no_blank_rows_pass(self) -> None:
        output = io.StringIO()

        with redirect_stdout(output):
            passed = check_blank_work_overlays()

        assert passed is True
        assert output.getvalue() == ""
