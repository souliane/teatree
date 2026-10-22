import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import CriticVerdict, PullRequest, Ticket
from teatree.core.models.critic_dispatch import CriticDispatch
from teatree.core.review.nit_lane import NitPoint, NitReceipt
from teatree.core.review.nit_publish import has_nit_receipts


class TestCriticRecordCommand(TestCase):
    def test_hand_critic_uses_the_factory_recording_path(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEW_REQUESTED)
        head = "a" * 40
        dispatch = CriticDispatch.enqueue(ticket=ticket, transition="merge", head_sha=head, contract="c")
        assert dispatch is not None
        envelope = {
            "critic_verdict": {
                "grader_identity": "critic-agent-7",
                "items": [
                    {"slug": "test_value", "status": "pass", "citation": "tests/test_example.py::test_behavior"},
                    {"slug": "cleanliness", "status": "pass", "citation": "src/example.py:8"},
                ],
            }
        }
        with TemporaryDirectory() as directory:
            path = Path(directory) / "verdict.json"
            path.write_text(json.dumps(envelope))
            receipt = call_command("ticket", "critic-record", str(ticket.pk), head, str(path))

        dispatch.refresh_from_db()
        verdict = CriticVerdict.objects.latest_for(ticket=ticket, transition="merge", head_sha=head)
        assert verdict is not None
        assert dispatch.state == CriticDispatch.State.RESOLVED
        assert isinstance(receipt, dict)
        assert receipt["verdict_id"] == verdict.pk

    def test_hand_critic_publishes_nits_and_returns_the_recorded_receipt(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEW_REQUESTED)
        head = "a" * 40
        pr_url = "https://github.com/souliane/teatree/pull/4040"
        PullRequest.objects.create(ticket=ticket, overlay="t3-teatree", url=pr_url, repo="souliane/teatree", iid="4040")
        dispatch = CriticDispatch.enqueue(ticket=ticket, transition="merge", head_sha=head, contract="c")
        assert dispatch is not None
        point = NitPoint("src/example.py", 8, "Rename helper")
        envelope = {
            "critic_verdict": {
                "grader_identity": "critic-agent-7",
                "items": [
                    {"slug": "test_value", "status": "pass", "citation": "tests/example.py::test_behavior"},
                    {
                        "slug": "cleanliness",
                        "status": "pass",
                        "citation": "src/example.py:8",
                        "points": [{"kind": "nit", "summary": point.summary, "file": point.file, "line": point.line}],
                    },
                ],
            }
        }
        with TemporaryDirectory() as directory:
            path = Path(directory) / "verdict.json"
            path.write_text(json.dumps(envelope))
            with (
                patch("teatree.core.review.nit_publish.get_overlay_for_ticket", return_value=object()),
                patch("teatree.core.review.nit_publish.get_backend_provider") as provider,
                patch(
                    "teatree.core.review.nit_publish.track_nits",
                    return_value=NitReceipt("https://github.com/souliane/teatree/issues/10", ("thread-1",), head),
                ),
            ):
                provider.return_value.get_code_host_for_url.return_value = object()
                receipt = call_command("ticket", "critic-record", str(ticket.pk), head, str(path))
        assert isinstance(receipt, dict)
        assert receipt["recorded"] is True
        ticket.refresh_from_db()
        assert has_nit_receipts(ticket, pr_url, head, (point,))
