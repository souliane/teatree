# test-path: cross-cutting
"""The command a peer's tunnel fields render — the one thing that opens its forward.

Both transports are pinned here because they differ in shape, not just in host: a box with
no public IP is reached by brokering ssh through IAP, and the arguments land on different
sides of the ``--`` separator. The hosts and options below are placeholders on purpose —
every real coordinate lives in the ``peer_instances`` registry, never in this repository.
"""

from teatree.config import PeerTransport, PeerTunnel


class TestTheForwardRefusesToOutliveItsOwnBind:
    """Without this, ssh stays connected when the near end cannot bind and nothing says so."""

    def test_a_plain_ssh_forward_exits_when_the_bind_fails(self) -> None:
        assert "-o ExitOnForwardFailure=yes" in PeerTunnel(host="box-b.example.invalid").command(9401)

    def test_a_brokered_forward_exits_when_the_bind_fails(self) -> None:
        tunnel = PeerTunnel(host="box-c.example.invalid", transport=PeerTransport.GCLOUD_IAP)

        assert "-o ExitOnForwardFailure=yes" in tunnel.command(9402)

    def test_the_option_reaches_ssh_not_the_broker(self) -> None:
        # `gcloud` would reject it as its own flag; it belongs after the `--` separator.
        rendered = PeerTunnel(host="box-c.example.invalid", transport=PeerTransport.GCLOUD_IAP).command(9402)

        assert rendered.index("--tunnel-through-iap") < rendered.index("-o ExitOnForwardFailure=yes")


class TestTheForwardNeverBackgroundsItself:
    """``-f`` would fork away the pid the runner tracks, leaving a forward it cannot close."""

    def test_plain_ssh_stays_in_the_foreground(self) -> None:
        assert " -f " not in PeerTunnel(host="box-b.example.invalid").command(9401)

    def test_the_brokered_forward_stays_in_the_foreground(self) -> None:
        tunnel = PeerTunnel(host="box-c.example.invalid", transport=PeerTransport.GCLOUD_IAP)

        assert " -f " not in tunnel.command(9402)


class TestTheBrokeredTransportOpensTheForwardFromInsideTheInstance:
    """``start-iap-tunnel`` dials the instance's internal NIC, which a loopback bind refuses."""

    def test_the_broker_carries_an_ssh_session_rather_than_a_raw_tcp_tunnel(self) -> None:
        rendered = PeerTunnel(host="box-c.example.invalid", transport=PeerTransport.GCLOUD_IAP).command(9402)

        assert rendered.startswith("gcloud compute ssh ")
        assert "start-iap-tunnel" not in rendered

    def test_the_transports_own_arguments_precede_the_separator(self) -> None:
        tunnel = PeerTunnel(
            host="box-c.example.invalid",
            transport=PeerTransport.GCLOUD_IAP,
            options=("--project", "test-project", "--zone", "test-zone"),
        )

        broker, forward = tunnel.command(9402).split(" -- ", 1)

        assert "--project test-project --zone test-zone" in broker
        assert forward.endswith("-L 9402:127.0.0.1:8000")
