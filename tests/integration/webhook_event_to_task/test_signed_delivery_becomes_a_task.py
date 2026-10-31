"""ACCEPTANCE (#5377) — a signed GitHub delivery with a registered handler ends as a Task.

The whole intake slice in one path: the real view verifies and stores the delivery, the
commit enqueues the wake, the wake task body runs the real dispatch and persistence, and
the stored row is settled identity-only. When the wake cannot be enqueued, the next
scanner drain reaches the same end.
"""

import hashlib
import hmac
import json
from unittest.mock import patch

from django.db import OperationalError
from django.tasks import TaskResultStatus
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

from teatree.core.models import IncomingEvent, Task, Ticket
from teatree.loop.domain_jobs import _global_dispatch_jobs
from teatree.loops import incoming_event_wake
from teatree.utils import secrets
from tests._incoming_event_handlers import ISSUE_URL, TASKS_SETTING, loop_row, preset_admitting, registered

SECRET = "e2e-secret"
KEY = "teatree/github-webhook/repository-101"


@override_settings(USE_TZ=True, TASKS=TASKS_SETTING)
class TestSignedDeliveryBecomesATask(TestCase):
    def setUp(self) -> None:
        loop_row()
        preset_admitting(admitted=True)
        self.enterContext(
            patch.multiple(
                secrets,
                read_pass=lambda key: SECRET if key == KEY else "",
                pass_entry_names=lambda prefix: frozenset({"repository-101"}),
            )
        )

    def deliver(self) -> int:
        body = json.dumps(
            {
                "action": "submitted",
                "repository": {"id": 101, "full_name": "acme/widgets"},
                "review": {"state": "approved", "submitted_at": timezone.now().isoformat()},
            }
        ).encode()
        signature = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("teatree:github_webhook"),
                data=body,
                content_type="application/json",
                headers={
                    "X-GitHub-Delivery": "e2e-1",
                    "X-GitHub-Event": "pull_request_review",
                    "X-GitHub-Hook-Installation-Target-Type": "repository",
                    "X-GitHub-Hook-Installation-Target-ID": "101",
                    "X-Hub-Signature-256": signature,
                },
            )
        return response.status_code

    def test_the_wake_task_turns_the_stored_delivery_into_a_task_and_settles_the_row(self) -> None:
        with registered():
            assert self.deliver() == 200
            wake = DBTaskResult.objects.get(
                task_path=incoming_event_wake.wake_incoming_event.module_path, status=TaskResultStatus.READY
            )
            assert not Task.objects.exists()

            incoming_event_wake.wake_incoming_event.call(**wake.args_kwargs["kwargs"])

        row = IncomingEvent.objects.get()
        assert row.processed_at is not None
        assert (row.body, row.payload_json) == ("", {})
        assert Ticket.objects.get().issue_url == ISSUE_URL
        assert Task.objects.count() == 1

    def test_when_the_wake_cannot_be_enqueued_the_next_scanner_drain_makes_the_task(self) -> None:
        with registered(), patch.object(incoming_event_wake, "wake_incoming_event") as wake:
            wake.enqueue.side_effect = OperationalError("database is locked")

            assert self.deliver() == 200
            assert IncomingEvent.objects.get().processed_at is None
            assert not Task.objects.exists()

            drain = next(job.scanner for job in _global_dispatch_jobs() if job.scanner.name == "incoming_events")
            drain.scan()

        assert IncomingEvent.objects.get().processed_at is not None
        assert Task.objects.count() == 1
