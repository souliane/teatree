"""The existing dashboard pages expose bounded, recorded factory telemetry."""

import hashlib
import json
import re
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from teatree.core.models.self_improve_firing import SelfImproveFiring
from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.models.ticket import Ticket
from teatree.core.telemetry.observation_read import ObservationRead
from teatree.dash.skills import skill_assurance
from teatree.dash.telemetry import build_telemetry_view
from tests.factories import TaskFactory, TicketFactory


class DashboardTelemetryTestCase(TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        directory_patch = patch("teatree.core.telemetry.admission._directory", return_value=self.directory)
        directory_patch.start()
        self.addCleanup(directory_patch.stop)

    def _factory_row(self, *, kind: str, cause: str, age: timedelta, key: str) -> str:
        observed = timezone.now() - age
        incident_id = hashlib.sha256(key.encode()).hexdigest()[:16]
        row = {
            "epoch": int(observed.timestamp()),
            "kind": kind,
            "cause": cause,
            "severity": "error",
            "count": 2,
            "incident_id": incident_id,
        }
        path = self.directory / f"factory-{observed.date().isoformat()}.jsonl"
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")
        return incident_id

    def test_live_page_shows_active_runtime_cause_age_and_recorded_action(self) -> None:
        key = "task-failed:42"
        incident_id = self._factory_row(kind="task_failed", cause="harness_crash", age=timedelta(minutes=3), key=key)
        ticket = TicketFactory(state=Ticket.State.STARTED)
        SelfImproveFiring.objects.create(
            detector="task_failure",
            dedup_key=key,
            dedup_key_digest=incident_id,
            state_hash="state",
            severity="error",
            last_action=SelfImproveFiring.Action.TICKET,
            ticket=ticket,
        )

        body = self.client.get(reverse("dash:live")).content.decode()

        assert "harness_crash" in body
        assert "task_failed" in body
        assert "3m" in body
        assert "ticket" in body
        assert f"#card-{ticket.pk}" in body

    def test_health_page_and_poll_show_boot_failure_without_worker(self) -> None:
        self._factory_row(kind="boot", cause="missing-skills", age=timedelta(minutes=2), key="boot-failure")

        for route in ("dash:health", "dash:health_bands"):
            body = self.client.get(reverse(route)).content.decode()
            assert "missing-skills" in body
            assert "boot" in body

    def test_health_page_calls_out_incomplete_telemetry_with_stale_event(self) -> None:
        self._factory_row(kind="task_failed", cause="harness_crash", age=timedelta(minutes=20), key="old-issue")

        body = self.client.get(reverse("dash:health")).content.decode()

        assert "Factory events" in body
        assert "Pressure events" in body
        assert "Lifecycle events" in body
        assert re.search(r"Factory events:\s+degraded \(event stream unavailable or incomplete; last valid event", body)

    def test_missing_stream_is_visibly_degraded_not_unobserved(self) -> None:
        view = build_telemetry_view()

        assert [(source.name, source.status) for source in view.sources] == [
            ("Factory events", "degraded"),
            ("Pressure events", "degraded"),
            ("Lifecycle events", "degraded"),
        ]
        body = self.client.get(reverse("dash:health")).content.decode()
        assert re.search(r"Factory events:\s+degraded", body)
        assert re.search(r"Pressure events:\s+degraded", body)
        assert re.search(r"Lifecycle events:\s+degraded", body)
        assert "no event in the last 24 hours" not in body

    def test_partial_stream_with_valid_event_is_degraded_not_current(self) -> None:
        now = timezone.now()
        factory = [
            {
                "epoch": int((now - timedelta(minutes=1)).timestamp()),
                "kind": "boot",
                "cause": "ready",
                "severity": "info",
                "count": 1,
                "incident_id": "0123456789abcdef",
            }
        ]
        with (
            patch(
                "teatree.dash.telemetry.checked_factory_observations",
                return_value=ObservationRead(factory, complete=False, reason="otel_truncated"),
            ),
            patch(
                "teatree.dash.telemetry.checked_pressure_observations", return_value=ObservationRead([], complete=True)
            ),
            patch(
                "teatree.dash.telemetry.checked_lifecycle_observations", return_value=ObservationRead([], complete=True)
            ),
        ):
            view = build_telemetry_view(now=now)

        assert [(source.name, source.status) for source in view.sources] == [
            ("Factory events", "degraded"),
            ("Pressure events", "unobserved"),
            ("Lifecycle events", "unobserved"),
        ]
        assert view.sources[0].last_seen is not None

    def test_a_successful_boot_observation_clears_an_earlier_boot_failure(self) -> None:
        failed_at = int((timezone.now() - timedelta(minutes=4)).timestamp())
        ready_at = failed_at + 120
        rows = [
            {
                "epoch": failed_at,
                "kind": "boot",
                "cause": "missing-skills",
                "severity": "error",
                "count": 1,
                "incident_id": "0123456789abcdef",
            },
            {
                "epoch": ready_at,
                "kind": "boot",
                "cause": "ready",
                "severity": "info",
                "count": 1,
                "incident_id": "fedcba9876543210",
            },
        ]
        with patch(
            "teatree.dash.telemetry.checked_factory_observations", return_value=ObservationRead(rows, complete=True)
        ):
            view = build_telemetry_view()

        assert view.sources[0].status == "current"
        assert view.incidents == ()

    def test_a_fresh_pressure_event_does_not_mask_stale_factory_events(self) -> None:
        now = timezone.now()
        factory = [
            {
                "epoch": int((now - timedelta(minutes=20)).timestamp()),
                "kind": "boot",
                "cause": "ready",
                "severity": "info",
            }
        ]
        pressure = [{"epoch": int((now - timedelta(minutes=1)).timestamp())}]
        with (
            patch(
                "teatree.dash.telemetry.checked_factory_observations",
                return_value=ObservationRead(factory, complete=True),
            ),
            patch(
                "teatree.dash.telemetry.checked_pressure_observations",
                return_value=ObservationRead(pressure, complete=True),
            ),
            patch(
                "teatree.dash.telemetry.checked_lifecycle_observations", return_value=ObservationRead([], complete=True)
            ),
        ):
            view = build_telemetry_view(now=now)

        assert [(source.name, source.status) for source in view.sources] == [
            ("Factory events", "stale"),
            ("Pressure events", "current"),
            ("Lifecycle events", "unobserved"),
        ]

    def test_a_durable_resolution_hides_an_older_runtime_observation(self) -> None:
        key = "task-failed:42"
        incident_id = self._factory_row(kind="task_failed", cause="harness_crash", age=timedelta(minutes=3), key=key)
        SelfImproveFiring.objects.create(
            detector="task_failure",
            dedup_key=key,
            dedup_key_digest=incident_id,
            state_hash="state",
            severity="error",
            resolved_at=timezone.now(),
        )

        assert build_telemetry_view().incidents == ()

    def test_reopened_incident_does_not_reuse_closed_generation_age_or_action(self) -> None:
        key = "reopened-task"
        incident_id = self._factory_row(kind="task_failed", cause="harness_crash", age=timedelta(minutes=2), key=key)
        old_ticket = TicketFactory(state=Ticket.State.STARTED)
        SelfImproveFiring.objects.create(
            detector="task_failure",
            dedup_key=key,
            dedup_key_digest=incident_id,
            state_hash="old-state",
            severity="error",
            first_fired_at=timezone.now() - timedelta(days=1),
            resolved_at=timezone.now() - timedelta(minutes=5),
            last_action=SelfImproveFiring.Action.TICKET,
            ticket=old_ticket,
        )

        row = build_telemetry_view().incidents[0]

        assert row.observed_for.startswith("2m")
        assert row.last_action == "none recorded"
        assert row.action_at is None
        assert row.action_ticket_id is None

    def test_resolved_newer_rows_do_not_hide_older_active_incident(self) -> None:
        active_id = self._factory_row(
            kind="task_failed", cause="harness_crash", age=timedelta(minutes=20), key="active"
        )
        for index in range(12):
            key = f"resolved-{index}"
            incident_id = self._factory_row(
                kind="task_failed", cause="harness_crash", age=timedelta(minutes=index + 1), key=key
            )
            SelfImproveFiring.objects.create(
                detector="task_failure",
                dedup_key=key,
                dedup_key_digest=incident_id,
                state_hash="closed",
                severity="error",
                resolved_at=timezone.now(),
            )

        incidents = build_telemetry_view().incidents

        assert len(incidents) == 1
        assert incidents[0].cause == "harness_crash"
        assert active_id not in {firing.dedup_key_digest for firing in SelfImproveFiring.objects.all()}

    def test_action_join_queries_only_observed_digests(self) -> None:
        incident_id = self._factory_row(
            kind="task_failed", cause="harness_crash", age=timedelta(minutes=3), key="active-issue"
        )
        SelfImproveFiring.objects.bulk_create(
            SelfImproveFiring(
                detector="task_failure",
                dedup_key=f"historic-{index}",
                dedup_key_digest=hashlib.sha256(f"historic-{index}".encode()).hexdigest()[:16],
                state_hash="state",
                severity="error",
            )
            for index in range(40)
        )

        with CaptureQueriesContext(connection) as queries:
            build_telemetry_view()

        firing_reads = [query["sql"] for query in queries if "teatree_self_improve_firing" in query["sql"]]
        assert len(firing_reads) == 1
        assert "dedup_key_digest" in firing_reads[0]
        assert incident_id in firing_reads[0]


class AttemptSkillAssuranceTestCase(TestCase):
    def test_empty_requested_set_cannot_claim_application(self) -> None:
        assurance = skill_assurance({"skill_assurance": {"requested": [], "status": "declared", "evidence": []}})

        assert assurance is not None
        assert assurance.status == "unverified"

    def _attempt(self, *, status: str, evidence: list[dict[str, str]]) -> TaskAttempt:
        task = TaskFactory(ticket=TicketFactory(state=Ticket.State.STARTED), phase="coding")
        return TaskAttempt.objects.create(
            task=task,
            agent_session_id="skill-session",
            skills_loaded=["t3:code"],
            result={
                "skill_assurance": {
                    "requested": ["t3:code", "t3:rules"],
                    "found": ["t3:code", "t3:rules"],
                    "injected": ["t3:code", "t3:rules"],
                    "explicit_load": ["t3:code"],
                    "missing": [],
                    "evidence": evidence,
                    "status": status,
                }
            },
        )

    def test_sessions_and_skills_show_each_recorded_assurance_stage(self) -> None:
        self._attempt(
            status="declared",
            evidence=[
                {"skill": "t3:code", "evidence": "applied"},
                {"skill": "t3:rules", "evidence": "applied"},
            ],
        )

        for route in ("dash:sessions", "dash:skills"):
            body = self.client.get(reverse(route)).content.decode()
            assert "t3:rules" in body
            assert "requested" in body.lower()
            assert "found" in body.lower()
            assert "injected" in body.lower()
            assert "agent-declared" in body.lower()

    def test_unverified_application_is_not_described_as_used(self) -> None:
        self._attempt(status="unverified", evidence=[])

        body = self.client.get(reverse("dash:sessions")).content.decode()

        assert "unverified" in body.lower()
        assert "agent-declared application" not in body.lower()

    def test_arbitrary_receipt_text_never_reaches_dashboard(self) -> None:
        self._attempt(status="declared", evidence=[{"skill": "t3:code", "evidence": "secret=top-secret-value"}])

        for route in ("dash:sessions", "dash:skills"):
            body = self.client.get(reverse(route)).content.decode()
            assert "top-secret-value" not in body

    def test_incomplete_declared_evidence_is_shown_as_unverified(self) -> None:
        self._attempt(status="declared", evidence=[{"skill": "t3:code", "evidence": "applied"}])

        body = self.client.get(reverse("dash:sessions")).content.decode()

        assert "unverified application" in body
        assert "agent-declared application" not in body


class DashboardRoutesSmokeTestCase(TestCase):
    def test_all_otel_consumer_pages_render(self) -> None:
        for route in ("dash:live", "dash:health", "dash:sessions", "dash:skills", "dash:cycle_time"):
            with self.subTest(route=route):
                assert self.client.get(reverse(route)).status_code == 200
