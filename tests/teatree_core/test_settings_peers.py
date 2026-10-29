from unittest.mock import patch

import httpx

from teatree.config import PeerInstance
from teatree.core.settings import settings_peers


def test_configured_peer_requires_an_import_without_a_network_request() -> None:
    peer = PeerInstance(name="box-b", url="http://127.0.0.1:9401/")
    with (
        patch.object(settings_peers, "load_peer_instances", return_value=[peer]),
        patch.object(httpx, "get") as get,
    ):
        rows = settings_peers.peer_snapshots()

    get.assert_not_called()
    assert [(row.label, row.error) for row in rows] == [("box-b", "import a saved snapshot to compare")]
