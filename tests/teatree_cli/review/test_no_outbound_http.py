"""The package-wide outbound-HTTP ban proves itself, so `attempts == 0` means something.

An autouse fixture that silently failed to patch is indistinguishable from one that
worked: every test still passes and every `attempts == 0` assertion still holds -- while
the requests go to the forge. So the ban gets one test of its own that drives a real
`httpx.Client` through the patched transport and watches it refuse.

It also has to count. `guarded_read` catches broadly, so the refusal raised inside a
gate's read is swallowed and the test passes anyway; the counter is the only thing that
survives that, and it is what the tests around it assert on.
"""

import httpx
import pytest

from teatree.cli.review.guarded_read import guarded_read
from tests.teatree_cli.review.conftest import OutboundHttpBan


class TestTheBanIsArmedAndCounts:
    def test_a_request_through_the_patched_transport_is_refused(self, no_outbound_http: OutboundHttpBan) -> None:
        with pytest.raises(AssertionError, match="outbound HTTP is banned"), httpx.Client() as client:
            client.get("https://gitlab.example.com/api/v4/user")

        assert no_outbound_http.attempts == 1

    def test_a_refusal_swallowed_by_a_guarded_read_still_counts(self, no_outbound_http: OutboundHttpBan) -> None:
        def _read() -> str:
            with httpx.Client() as client:
                return client.get("https://gitlab.example.com/api/v4/user").text

        outcome = guarded_read("a banned read", _read, neutral="")

        assert outcome.failed is True
        assert no_outbound_http.attempts == 1
