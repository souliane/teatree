"""The append-only intake attachment manifest (:class:`AttachmentManifest`, M5).

The guarded factory refuses a non-list ``entries`` or an unattributable author,
the row is append-only with "latest governs", and ``latest_for`` returns the
most recent snapshot. An *empty* manifest is legitimate (zero-attachment ticket).
Each assertion fails if the guard or the ordering regresses.
"""

import datetime

import pytest
from django.test import TestCase

from teatree.core.models import AttachmentManifest, Ticket


def _ticket() -> Ticket:
    return Ticket.objects.create(overlay="acme", role=Ticket.Role.AUTHOR)


_ENTRIES: list[dict[str, str]] = [
    {"source_url": "/uploads/" + "a" * 32 + "/spec.pdf", "kind": "gitlab-upload", "local_path": "", "fetched_at": ""},
    {
        "source_url": "https://www.notion.so/page-abc",
        "kind": "notion",
        "local_path": "/w/.attachments/x-mock.png",
        "fetched_at": "2026-07-04T00:00:00",
    },
]


class TestRecord(TestCase):
    def test_persists_the_entries_verbatim(self) -> None:
        ticket = _ticket()

        manifest = AttachmentManifest.record(ticket=ticket, entries=_ENTRIES, recorded_by="t3:intake")

        manifest.refresh_from_db()
        assert manifest.entries == _ENTRIES
        assert manifest.recorded_by == "t3:intake"

    def test_empty_manifest_is_legitimate(self) -> None:
        """A zero-attachment ticket records an empty manifest — not a refusal."""
        ticket = _ticket()

        manifest = AttachmentManifest.record(ticket=ticket, entries=[], recorded_by="t3:intake")

        manifest.refresh_from_db()
        assert manifest.entries == []

    def test_non_list_entries_is_refused(self) -> None:
        ticket = _ticket()
        not_a_list: object = {"source_url": "x"}

        with pytest.raises(TypeError, match="entries must be a list"):
            AttachmentManifest.record(ticket=ticket, entries=not_a_list, recorded_by="t3:intake")

        assert not AttachmentManifest.objects.filter(ticket=ticket).exists()

    def test_str_entries_is_refused(self) -> None:
        ticket = _ticket()
        # A bare string is a Sequence but not a list of dicts — the guard rejects
        # it so a stray string never persists as a one-char-per-entry manifest.
        stringy: object = "not-a-list"

        with pytest.raises(TypeError, match="entries must be a list"):
            AttachmentManifest.record(ticket=ticket, entries=stringy, recorded_by="t3:intake")

    def test_blank_author_is_refused(self) -> None:
        ticket = _ticket()

        with pytest.raises(ValueError, match="recorded_by is required"):
            AttachmentManifest.record(ticket=ticket, entries=_ENTRIES, recorded_by="   ")

        assert not AttachmentManifest.objects.filter(ticket=ticket).exists()


class TestLatestGoverns(TestCase):
    def test_is_append_only_and_latest_for_returns_the_newest(self) -> None:
        ticket = _ticket()
        older = [{"source_url": "/uploads/" + "b" * 32 + "/old.pdf", "kind": "gitlab-upload"}]
        newer = [{"source_url": "/uploads/" + "c" * 32 + "/new.pdf", "kind": "gitlab-upload"}]

        first = AttachmentManifest.record(ticket=ticket, entries=older, recorded_by="t3:intake")
        second = AttachmentManifest.record(ticket=ticket, entries=newer, recorded_by="t3:intake")
        AttachmentManifest.objects.filter(pk=second.pk).update(
            recorded_at=first.recorded_at + datetime.timedelta(seconds=1)
        )

        assert AttachmentManifest.objects.filter(ticket=ticket).count() == 2
        latest = AttachmentManifest.latest_for(ticket)
        assert latest is not None
        assert latest.entries[0]["source_url"].endswith("new.pdf")

    def test_latest_for_with_no_manifest_is_none(self) -> None:
        assert AttachmentManifest.latest_for(_ticket()) is None

    def test_str_names_ticket_and_entry_count(self) -> None:
        ticket = _ticket()
        manifest = AttachmentManifest.record(ticket=ticket, entries=_ENTRIES, recorded_by="t3:intake")
        rendered = str(manifest)
        assert "attachment-manifest" in rendered
        assert "2 entries" in rendered
