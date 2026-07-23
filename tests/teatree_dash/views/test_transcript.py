"""The opt-in transcript click-through view (#3673 Tier 2).

Loopback/staff-gated, GET-only, and never invoked during list rendering — the
drawer only links to it. A missing transcript renders an empty-state, never a 500.
"""

from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from teatree.dash.transcript import TranscriptEntry

_NON_LOOPBACK = "203.0.113.7"


class TranscriptViewTestCase(TestCase):
    def test_renders_redacted_entries(self) -> None:
        rows = [TranscriptEntry(role="assistant", text="a redacted line")]
        with patch("teatree.dash.views.transcript.tail_transcript", return_value=rows):
            resp = self.client.get(reverse("dash:transcript", args=["sess-x"]))
        assert resp.status_code == 200
        assert "a redacted line" in resp.content.decode()

    def test_missing_transcript_renders_empty_state_not_500(self) -> None:
        with patch("teatree.dash.views.transcript.tail_transcript", return_value=[]):
            resp = self.client.get(reverse("dash:transcript", args=["gone"]))
        assert resp.status_code == 200
        assert "no transcript" in resp.content.decode().lower()

    def test_non_loopback_is_forbidden(self) -> None:
        with patch("teatree.dash.views.transcript.tail_transcript", return_value=[]):
            resp = self.client.get(reverse("dash:transcript", args=["s"]), REMOTE_ADDR=_NON_LOOPBACK)
        assert resp.status_code == 403

    def test_post_is_rejected(self) -> None:
        resp = self.client.post(reverse("dash:transcript", args=["s"]))
        assert resp.status_code == 405
