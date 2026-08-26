"""``t3 peer`` — the verbs that open the forwards the Compare Instances page reads through.

The page computed each peer's tunnel command and printed it for someone to paste. These
verbs are the execution that was missing; what is pinned here is that they resolve every
coordinate from the registry, refuse rather than half-open, and echo nothing but the peer's
own label — the operator chose that, whereas the host it resolves to is what stays in config.
"""

from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from teatree.cli.admin import ADMIN_PATH, BROWSE_URL_FILE, DASHBOARD_PATH
from teatree.cli.peer import peer_app
from teatree.config import PeerInstance, PeerTransport, PeerTunnel
from teatree.core.peer_forward import PLAN_FILE

runner = CliRunner()

_PEERS = [
    PeerInstance(name="box-b", url="http://127.0.0.1:9401/", tunnel=PeerTunnel(host="box-b.example.invalid")),
    PeerInstance(
        name="box-c",
        url="http://127.0.0.1:9402/",
        tunnel=PeerTunnel(
            host="box-c.example.invalid",
            transport=PeerTransport.GCLOUD_IAP,
            options=("--project", "test-project", "--zone", "test-zone"),
        ),
    ),
]


@pytest.fixture
def registry():
    with patch("teatree.config.load_peer_instances", return_value=_PEERS):
        yield


@pytest.fixture
def containerized(tmp_path: Path):
    """The deployed venue: the CLI resolves the plan, the host wrapper carries it out."""
    with (
        patch("teatree.utils.ports.running_in_container", return_value=True),
        patch("teatree.core.peer_forward.data_dir_root", return_value=tmp_path),
        patch("teatree.paths.data_dir_root", return_value=tmp_path),
    ):
        yield tmp_path


def _browse_url(where: Path) -> str:
    return (where / BROWSE_URL_FILE).read_text().strip()


class TestUpResolvesEveryCoordinateFromTheRegistry:
    def test_one_named_peer_plans_only_that_peer(self, registry, containerized: Path) -> None:
        result = runner.invoke(peer_app, ["up", "box-b"])

        assert result.exit_code == 0
        planned = (containerized / PLAN_FILE).read_text().splitlines()[2:]
        assert [row.split("\t", 1)[0] for row in planned] == ["box-b"]

    def test_no_name_plans_every_registered_peer(self, registry, containerized: Path) -> None:
        result = runner.invoke(peer_app, ["up"])

        assert result.exit_code == 0
        planned = (containerized / PLAN_FILE).read_text().splitlines()[2:]
        assert [row.split("\t", 1)[0] for row in planned] == ["box-b", "box-c"]

    def test_the_planned_command_is_the_one_the_registry_renders(self, registry, containerized: Path) -> None:
        runner.invoke(peer_app, ["up", "box-c"])

        assert (containerized / PLAN_FILE).read_text().rstrip().endswith(_PEERS[1].tunnel_command)


class TestAnUnknownPeerIsRefusedRatherThanSilentlyDoingNothing:
    def test_a_name_no_peer_carries_exits_nonzero(self, registry, containerized: Path) -> None:
        # Doing nothing quietly reads exactly like a forward that opened, which is the worse
        # of the two answers for an operator whose page is still showing refused.
        result = runner.invoke(peer_app, ["up", "box-z"])

        assert result.exit_code != 0

    def test_the_refusal_names_the_verb_that_lists_the_real_ones(self, registry, containerized: Path) -> None:
        assert "peer list" in runner.invoke(peer_app, ["up", "box-z"]).output


class TestAPeerThatDeclaresNoTunnelRefusesTheWholeRun:
    _NO_TUNNEL: ClassVar[list[PeerInstance]] = [*_PEERS, PeerInstance(name="box-d", url="http://127.0.0.1:9403/")]

    def test_the_run_is_refused_rather_than_partly_carried_out(self, containerized: Path) -> None:
        with patch("teatree.config.load_peer_instances", return_value=self._NO_TUNNEL):
            result = runner.invoke(peer_app, ["up"])

        assert result.exit_code != 0
        assert not (containerized / PLAN_FILE).exists()


class TestListNamesWhatEachPeerCanAndCannotDo:
    def test_a_peer_with_a_tunnel_shows_the_port_its_forward_lands_on(self, registry) -> None:
        assert "127.0.0.1:9401" in runner.invoke(peer_app, ["list"]).output

    def test_a_peer_without_one_says_nothing_can_open_its_forward(self) -> None:
        with patch(
            "teatree.config.load_peer_instances", return_value=[PeerInstance("box-d", "http://127.0.0.1:9403/")]
        ):
            assert "NO tunnel" in runner.invoke(peer_app, ["list"]).output

    def test_an_empty_registry_says_so_rather_than_printing_nothing(self) -> None:
        with patch("teatree.config.load_peer_instances", return_value=[]):
            result = runner.invoke(peer_app, ["list"])

        assert result.exit_code == 0
        assert "no peers are registered" in result.output


class TestOpenBringsTheForwardUpBeforeReachingForThePage:
    def test_the_forward_is_planned_not_merely_the_url_recorded(self, registry, containerized: Path) -> None:
        # The page does not answer until a forward is up, so recording the url alone would
        # open a browser onto a refused connection.
        runner.invoke(peer_app, ["open", "box-b"])

        assert (containerized / PLAN_FILE).read_text().startswith("action=up")

    def test_the_board_is_the_page_it_reaches_by_default(self, registry, containerized: Path) -> None:
        runner.invoke(peer_app, ["open", "box-b"])

        assert _browse_url(containerized) == f"http://127.0.0.1:9401{DASHBOARD_PATH}"

    def test_the_admin_is_reachable_through_the_same_forward(self, registry, containerized: Path) -> None:
        runner.invoke(peer_app, ["open", "box-b", "--admin"])

        assert _browse_url(containerized) == f"http://127.0.0.1:9401{ADMIN_PATH}"

    def test_the_url_lands_on_the_port_that_peers_own_forward_binds(self, registry, containerized: Path) -> None:
        runner.invoke(peer_app, ["open", "box-c"])

        assert _browse_url(containerized).startswith("http://127.0.0.1:9402/")

    def test_no_coordinate_beyond_the_label_reaches_the_output(self, registry, containerized: Path) -> None:
        output = runner.invoke(peer_app, ["up", "box-c"]).output

        assert "box-c" in output
        assert "box-c.example.invalid" not in output
        assert "test-project" not in output
