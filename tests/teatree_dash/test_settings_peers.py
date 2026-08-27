"""Where a peer's loopback url actually resolves from the process doing the fetching.

A peer's url names the near end of a forward, and a forward is opened where the credentials
that open it live — the operator's own machine. The dashboard doing the fetching does not
necessarily run there: under the deploy stack it runs in a container, whose ``127.0.0.1`` is
its own and has nothing listening on it. Every peer then reads as refused while the forward
is demonstrably up, which is indistinguishable from a tunnel that never opened.

So the url is resolved for the venue before it is fetched, and shown as configured — the
operator reads the address their own forward binds, not this process's route to it. The name
the peer is ASKED by is preserved across that swap: the peer's own ``ALLOWED_HOSTS`` knows the
loopback it binds and nothing else, so a rewritten Host header turns the refusal into a 400.
"""

from unittest.mock import patch

import pytest

from teatree.dash import settings_peers
from teatree.dash.settings_peers import PeerSnapshot, fetch_target, snapshot_url


class TestALoopbackPeerIsFetchedWhereThisProcessCanReachIt:
    @pytest.fixture(autouse=True)
    def _in_a_container(self):
        with patch.object(settings_peers, "host_published_port_host", return_value="host.docker.internal"):
            yield

    def test_the_loopback_host_becomes_this_processs_route_to_it(self) -> None:
        assert fetch_target("http://127.0.0.1:9401/x").url == "http://host.docker.internal:9401/x"

    def test_the_written_out_name_for_it_is_resolved_too(self) -> None:
        assert fetch_target("http://localhost:9401/x").url == "http://host.docker.internal:9401/x"

    def test_the_port_is_carried_over_untouched(self) -> None:
        # The forward binds the port the registry named; resolving the host may not move it.
        assert ":9402/" in fetch_target("http://127.0.0.1:9402/dash/settings/snapshot.json").url

    def test_the_peer_is_still_asked_by_the_name_it_answers_to(self) -> None:
        # The peer's ALLOWED_HOSTS knows the loopback it binds; asked by anything else it 400s,
        # which reads as a broken peer rather than a header this side rewrote.
        assert fetch_target("http://127.0.0.1:9401/x").headers["Host"] == "127.0.0.1:9401"

    def test_a_peer_named_by_something_other_than_loopback_is_left_alone(self) -> None:
        target = fetch_target("http://box-b.example.invalid:9401/x")

        assert target.url == "http://box-b.example.invalid:9401/x"
        assert not target.headers


class TestNativelyTheUrlIsUntouched:
    def test_a_loopback_url_is_already_this_processs_route_to_it(self) -> None:
        with patch.object(settings_peers, "host_published_port_host", return_value="localhost"):
            target = fetch_target("http://127.0.0.1:9401/x")

        assert target.url == "http://127.0.0.1:9401/x"
        assert not target.headers


class TestTheRowShowsTheAddressTheOperatorsForwardBinds:
    def test_the_url_on_the_row_is_the_configured_one_not_the_resolved_one(self) -> None:
        with (
            patch.object(settings_peers, "host_published_port_host", return_value="host.docker.internal"),
            patch.object(settings_peers.httpx, "get", side_effect=OSError("refused")),
        ):
            row = settings_peers._fetch("box-b", "http://127.0.0.1:9401/", "", 1.0)

        assert row.url == snapshot_url("http://127.0.0.1:9401/")

    def test_the_fetch_asks_by_the_configured_name_over_the_resolved_route(self) -> None:
        with (
            patch.object(settings_peers, "host_published_port_host", return_value="host.docker.internal"),
            patch.object(settings_peers.httpx, "get", side_effect=OSError("refused")) as fetched,
        ):
            settings_peers._fetch("box-b", "http://127.0.0.1:9401/", "", 1.0)

        assert fetched.call_args.args[0].startswith("http://host.docker.internal:9401/")
        assert fetched.call_args.kwargs["headers"] == {"Host": "127.0.0.1:9401"}

    def test_a_peer_fetched_through_the_resolved_route_still_carries_its_own_label(self) -> None:
        assert PeerSnapshot(label="box-b", url="", note="").label == "box-b"
