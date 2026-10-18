from types import SimpleNamespace
from unittest.mock import patch

from teatree.loop.scanner_factory_config import _user_slack_id_for_overlay


def test_unreadable_overlay_registry_returns_empty_requester_id() -> None:
    with patch("teatree.config.load_config", side_effect=RuntimeError("config unreadable")):
        assert _user_slack_id_for_overlay("t3-test") == ""


def test_unreadable_global_setting_returns_empty_requester_id() -> None:
    with (
        patch("teatree.config.load_config", return_value=SimpleNamespace(raw={"overlays": {}})),
        patch("teatree.config.cold_reader.str_setting", side_effect=RuntimeError("db unreadable")),
    ):
        assert _user_slack_id_for_overlay("t3-test") == ""
