"""Resolving a peer's forward into a plan the host runner can carry out.

The resolving half is all that lives in Python: opening the forward is a host act, so what
is pinned here is that a plan is only ever built from what the registry SAYS, that a peer
whose entry cannot produce one is refused by name rather than half-opened, and that no
coordinate beyond the peer's own label reaches a message an operator reads.
"""

from pathlib import Path

import pytest

from teatree.config import PeerInstance, PeerTransport, PeerTunnel
from teatree.core.peer_forward import ForwardAction, ForwardPlan, forward_plan, runner_path, write_plan

_SSH_PEER = PeerInstance(name="box-b", url="http://127.0.0.1:9401/", tunnel=PeerTunnel(host="box-b.example.invalid"))


class TestAPlanIsRenderedFromTheRegistryAlone:
    def test_the_port_is_read_off_the_peers_own_url(self) -> None:
        assert forward_plan(_SSH_PEER).port == 9401

    def test_the_command_opens_that_same_port(self) -> None:
        assert "9401:127.0.0.1:8000" in forward_plan(_SSH_PEER).argv

    def test_the_plan_is_labelled_by_the_registry_key(self) -> None:
        assert forward_plan(_SSH_PEER).peer == "box-b"


class TestAPeerThatCannotProduceAPlanIsRefusedByName:
    def test_a_peer_declaring_no_tunnel_is_refused(self) -> None:
        peer = PeerInstance(name="box-c", url="http://127.0.0.1:9402/")

        with pytest.raises(ValueError, match="box-c"):
            forward_plan(peer)

    def test_a_url_naming_no_port_is_refused(self) -> None:
        peer = PeerInstance(name="box-d", url="http://127.0.0.1/", tunnel=PeerTunnel(host="box-d.example.invalid"))

        with pytest.raises(ValueError, match="box-d"):
            forward_plan(peer)

    def test_the_refusal_never_echoes_the_resolved_host(self) -> None:
        peer = PeerInstance(name="box-e", url="http://127.0.0.1/", tunnel=PeerTunnel(host="secret.example.invalid"))

        with pytest.raises(ValueError, match="box-e") as caught:
            forward_plan(peer)

        assert "secret.example.invalid" not in str(caught.value)


class TestOnlyOpeningAForwardNeedsATunnel:
    """``status`` and ``down`` read the port; a peer declaring no tunnel is theirs to report on.

    Requiring one for every action refused the whole run over a peer whose registry entry is
    merely incomplete — a state ``t3 peer list`` prints as expected — so the peers that ARE
    configured could not be reported on either.
    """

    _NO_TUNNEL = PeerInstance(name="box-f", url="http://127.0.0.1:9403/")

    @pytest.mark.parametrize("action", [ForwardAction.STATUS, ForwardAction.DOWN])
    def test_a_peer_with_no_tunnel_still_gets_a_plan(self, action: ForwardAction) -> None:
        assert forward_plan(self._NO_TUNNEL, action=action).port == 9403

    @pytest.mark.parametrize("action", [ForwardAction.STATUS, ForwardAction.DOWN])
    def test_that_plan_carries_no_argv_because_neither_verb_reads_one(self, action: ForwardAction) -> None:
        assert forward_plan(self._NO_TUNNEL, action=action).argv == ()

    def test_opening_one_is_still_refused_by_name(self) -> None:
        with pytest.raises(ValueError, match="box-f"):
            forward_plan(self._NO_TUNNEL, action=ForwardAction.UP)

    @pytest.mark.parametrize("action", list(ForwardAction))
    def test_a_url_naming_no_port_is_refused_whatever_the_action(self, action: ForwardAction) -> None:
        # The port is what every verb reports on, so its absence is fatal to all three.
        peer = PeerInstance(name="box-g", url="http://127.0.0.1/")

        with pytest.raises(ValueError, match="box-g"):
            forward_plan(peer, action=action)


class TestThePlanFileIsWhatTheRunnerParses:
    def test_the_argv_is_last_so_its_own_length_cannot_shift_a_field(self) -> None:
        row = ForwardPlan(peer="box-b", port=9401, argv=("ssh", "-N", "-L", "9401:127.0.0.1:8000", "box-b")).as_row()

        assert row.split("\t")[:2] == ["box-b", "9401"]

    def test_a_registry_token_carrying_a_tab_is_refused_rather_than_forging_a_field(self) -> None:
        # The row is TAB-delimited, so a tab inside one token would otherwise let a
        # peer_instances string invent argv the registry never declared.
        plan = ForwardPlan(peer="box-b", port=9401, argv=("ssh", "box\textra"))

        with pytest.raises(ValueError, match="box-b"):
            plan.as_row()

    def test_the_action_and_wait_head_the_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.core.peer_forward.data_dir_root", lambda: tmp_path)

        written = write_plan(ForwardAction.UP, [forward_plan(_SSH_PEER)], wait_seconds=3.0).read_text()

        assert written.splitlines()[:2] == ["action=up", "wait_seconds=3.0"]

    def test_every_planned_peer_gets_a_row(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.core.peer_forward.data_dir_root", lambda: tmp_path)
        other = PeerInstance(
            name="box-c",
            url="http://127.0.0.1:9402/",
            tunnel=PeerTunnel(host="box-c.example.invalid", transport=PeerTransport.GCLOUD_IAP),
        )

        written = write_plan(ForwardAction.UP, [forward_plan(_SSH_PEER), forward_plan(other)]).read_text()

        assert [line.split("\t", 1)[0] for line in written.splitlines()[2:]] == ["box-b", "box-c"]


def test_the_runner_the_cli_hands_the_plan_to_is_the_one_on_disk() -> None:
    # A path that resolves to nothing would only be discovered by an operator whose forward
    # silently never opened, so it is asserted rather than assumed.
    assert runner_path().is_file()
