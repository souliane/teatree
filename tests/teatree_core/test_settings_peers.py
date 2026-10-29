from unittest.mock import patch

import httpx

from teatree.config import PeerInstance
from teatree.core.settings import settings_peers


def test_peer_login_redirect_reports_sign_in_and_keeps_live_url() -> None:
    peer = PeerInstance(name="box-b", url="http://127.0.0.1:9401/")
    response = httpx.Response(
        302,
        headers={"Location": "/login/?next=/dash/settings/snapshot/"},
        request=httpx.Request("GET", "http://127.0.0.1:9401/dash/settings/snapshot.json"),
    )
    with (
        patch.object(settings_peers, "load_peer_instances", return_value=[peer]),
        patch.object(httpx.Client, "get", return_value=response) as get,
    ):
        rows = settings_peers.peer_snapshots()

    get.assert_called_once()
    assert rows[0].label == "box-b"
    assert rows[0].url.endswith("/dash/settings/snapshot.json")
    assert rows[0].error == "peer requires sign-in"
