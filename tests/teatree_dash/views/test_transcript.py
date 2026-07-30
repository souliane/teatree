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


class TranscriptStandalonePageTestCase(TestCase):
    """The drawer links this with a real ``href``, so it must survive a plain navigation.

    New-tab, middle-click and JS-off all issue an ordinary GET. Answering the bare
    htmx fragment to those gives an unstyled ``<div>`` with no layout, no nav and no
    way back — the fragment is correct only when htmx asked for it.
    """

    def _get(self, *, htmx: bool) -> object:
        headers = {"HTTP_HX_REQUEST": "true"} if htmx else {}
        with patch("teatree.dash.views.transcript.tail_transcript", return_value=[]):
            return self.client.get(reverse("dash:transcript", args=["sess-x"]), **headers)

    def test_a_plain_navigation_renders_a_full_page(self) -> None:
        body = self._get(htmx=False).content.decode()
        assert "<!doctype html>" in body.lower()
        assert 'href="#dash-main"' in body
        assert "dash/css/dash.css" in body

    def test_an_htmx_request_still_gets_the_bare_fragment(self) -> None:
        body = self._get(htmx=True).content.decode()
        assert "<!doctype html>" not in body.lower()
        assert "transcript" in body.lower()
