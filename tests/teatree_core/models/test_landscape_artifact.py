"""The append-only intake landscape artifact (:class:`LandscapeArtifact`, #2541).

The guarded factory refuses a vacuous survey or an unattributable author, the row
is append-only with "latest governs", and ``latest_for`` returns the most recent
survey. Each assertion fails if the guard or the ordering regresses.
"""

import pytest
from django.test import TestCase

from teatree.core.models import LandscapeArtifact, Ticket


def _ticket() -> Ticket:
    return Ticket.objects.create(overlay="acme", role=Ticket.Role.AUTHOR)


_SURVEY = {
    "worktrees": [{"path": "/w/1", "branch": "b", "has_uncommitted": True, "has_unpushed": False, "in_flight": True}],
    "open_prs": [{"url": "https://forge/pr/1", "title": "WIP (#50)", "referenced_issues": [50]}],
    "recommendations": [
        {
            "issue_url": "https://forge/issues/50",
            "title": "x",
            "disposition": "partial",
            "action": "merge",
            "evidence": "https://forge/pr/1",
        }
    ],
    "warnings": [],
}


class TestRecord(TestCase):
    def test_persists_the_survey_payload_verbatim(self) -> None:
        ticket = _ticket()

        artifact = LandscapeArtifact.record(ticket=ticket, survey=_SURVEY, recorded_by="t3:intake")

        artifact.refresh_from_db()
        assert artifact.survey == _SURVEY
        assert artifact.recorded_by == "t3:intake"

    def test_empty_survey_is_refused(self) -> None:
        ticket = _ticket()

        with pytest.raises(ValueError, match="survey is required"):
            LandscapeArtifact.record(ticket=ticket, survey={}, recorded_by="t3:intake")

        assert not LandscapeArtifact.objects.filter(ticket=ticket).exists()

    def test_non_dict_survey_is_refused(self) -> None:
        ticket = _ticket()
        # A non-Mapping payload (e.g. a list) carries no survey shape; the guard
        # rejects it at runtime. Typed as object so the test exercises the
        # runtime branch without a type-ignore.
        not_a_mapping: object = ["not", "a", "dict"]

        with pytest.raises(ValueError, match="survey is required"):
            LandscapeArtifact.record(ticket=ticket, survey=not_a_mapping, recorded_by="t3:intake")

    def test_blank_author_is_refused(self) -> None:
        ticket = _ticket()

        with pytest.raises(ValueError, match="recorded_by is required"):
            LandscapeArtifact.record(ticket=ticket, survey=_SURVEY, recorded_by="   ")

        assert not LandscapeArtifact.objects.filter(ticket=ticket).exists()


class TestLatestGoverns(TestCase):
    def test_is_append_only_and_latest_for_returns_the_newest(self) -> None:
        ticket = _ticket()
        older = {**_SURVEY, "warnings": ["older run"]}
        newer = {**_SURVEY, "warnings": ["newer run"]}

        first = LandscapeArtifact.record(ticket=ticket, survey=older, recorded_by="t3:intake")
        # Force a strictly-later recorded_at so ordering is unambiguous.
        second = LandscapeArtifact.record(ticket=ticket, survey=newer, recorded_by="t3:intake")
        LandscapeArtifact.objects.filter(pk=second.pk).update(
            recorded_at=first.recorded_at + __import__("datetime").timedelta(seconds=1)
        )

        assert LandscapeArtifact.objects.filter(ticket=ticket).count() == 2
        latest = LandscapeArtifact.latest_for(ticket)
        assert latest is not None
        assert latest.survey["warnings"] == ["newer run"]

    def test_latest_for_with_no_artifact_is_none(self) -> None:
        assert LandscapeArtifact.latest_for(_ticket()) is None
