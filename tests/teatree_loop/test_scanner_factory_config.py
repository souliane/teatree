from types import SimpleNamespace
from unittest.mock import patch

import pytest

from teatree.loop.scanner_factory_config import _user_slack_id_for_overlay


def test_unreadable_overlay_registry_raises_for_pr_sweep() -> None:
    with (
        patch("teatree.config.load_config", side_effect=RuntimeError("config unreadable")),
        pytest.raises(RuntimeError, match="config unreadable"),
    ):
        _user_slack_id_for_overlay("t3-test")


def test_unreadable_global_setting_raises_for_pr_sweep() -> None:
    with (
        patch("teatree.config.load_config", return_value=SimpleNamespace(raw={"overlays": {}})),
        patch("teatree.config.cold_reader.str_setting", side_effect=RuntimeError("db unreadable")),
        pytest.raises(RuntimeError, match="db unreadable"),
    ):
        _user_slack_id_for_overlay("t3-test")
