"""A merge reads a head-bound receipt for each published advisory point."""

from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.models import Ticket
from teatree.core.review.nit_lane import NitPoint, NitReceipt
from teatree.core.review.nit_publish import has_nit_receipts, publish_nits

_PR_URL = "https://github.com/souliane/teatree/pull/9"
_HEAD = "a" * 40
_POINT = NitPoint("src/example.py", 8, "Rename the helper")


class TestNitPublish(TestCase):
    def test_publish_records_a_receipt_only_after_forge_readback(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree")
        host = object()
        with (
            patch("teatree.core.review.nit_publish.get_overlay_for_ticket", return_value=object()),
            patch("teatree.core.review.nit_publish.get_backend_provider") as provider,
            patch(
                "teatree.core.review.nit_publish.track_nits",
                return_value=NitReceipt("https://github.com/souliane/teatree/issues/10", ("thread-1",), _HEAD),
            ) as track,
        ):
            provider.return_value.get_code_host_for_url.return_value = host
            assert not has_nit_receipts(ticket, _PR_URL, _HEAD, (_POINT,))
            publish_nits(ticket, _PR_URL, _HEAD, (_POINT,))

        track.assert_called_once_with(host=host, pr_url=_PR_URL, head_sha=_HEAD, points=(_POINT,))
        assert has_nit_receipts(ticket, _PR_URL, _HEAD, (_POINT,))
        assert not has_nit_receipts(ticket, _PR_URL, "b" * 40, (_POINT,))
        assert not has_nit_receipts(ticket, _PR_URL, _HEAD, (NitPoint("src/example.py", 8, "Another nit"),))

    def test_failed_publication_never_stamps_a_receipt(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree")
        with (
            patch("teatree.core.review.nit_publish.get_overlay_for_ticket", return_value=object()),
            patch("teatree.core.review.nit_publish.get_backend_provider") as provider,
            patch("teatree.core.review.nit_publish.track_nits", side_effect=RuntimeError("readback failed")),
        ):
            provider.return_value.get_code_host_for_url.return_value = object()
            with pytest.raises(RuntimeError, match="readback failed"):
                publish_nits(ticket, _PR_URL, _HEAD, (_POINT,))
        assert not has_nit_receipts(ticket, _PR_URL, _HEAD, (_POINT,))
