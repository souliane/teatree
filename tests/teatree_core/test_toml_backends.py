"""Behaviour of the path-only TOML overlay backend builders."""

from unittest.mock import patch

from teatree.core import toml_backends


def _config_with(overlays: dict[str, object]) -> object:
    return type("Cfg", (), {"raw": {"overlays": overlays}})()


class TestCfgStr:
    def test_reads_string_value(self) -> None:
        assert toml_backends._cfg_str({"gitlab_url": "https://gl"}, "gitlab_url") == "https://gl"

    def test_non_string_value_falls_back_to_default(self) -> None:
        assert toml_backends._cfg_str({"gitlab_url": 5}, "gitlab_url", "d") == "d"

    def test_absent_key_falls_back_to_default(self) -> None:
        assert toml_backends._cfg_str({}, "gitlab_url", "d") == "d"


class TestOverlayCfg:
    def test_blank_name_is_none(self) -> None:
        assert toml_backends._overlay_cfg("") is None

    def test_resolves_the_named_overlay_block(self) -> None:
        cfg = _config_with({"foo": {"gitlab_token_ref": "ref"}})
        with patch("teatree.config.load_config", return_value=cfg):
            assert toml_backends._overlay_cfg("foo") == {"gitlab_token_ref": "ref"}

    def test_non_dict_entry_is_none(self) -> None:
        cfg = _config_with({"foo": "not-a-dict"})
        with patch("teatree.config.load_config", return_value=cfg):
            assert toml_backends._overlay_cfg("foo") is None

    def test_absent_overlay_is_none(self) -> None:
        cfg = _config_with({})
        with patch("teatree.config.load_config", return_value=cfg):
            assert toml_backends._overlay_cfg("missing") is None


class TestTomlOverlayFallbacksShortCircuitOnMissingConfig:
    def test_messaging_from_toml_overlay_none_when_absent(self) -> None:
        assert toml_backends._messaging_from_toml_overlay("") is None

    def test_code_host_from_toml_overlay_none_when_absent(self) -> None:
        assert toml_backends._code_host_from_toml_overlay("") is None

    def test_code_host_for_repo_none_when_absent(self) -> None:
        assert toml_backends._code_host_from_toml_overlay_for_repo("", "/some/repo") is None

    def test_toml_messaging_backend_blank_when_absent(self) -> None:
        assert toml_backends._toml_messaging_backend("") == ""


class TestTomlMessagingBackendName:
    def test_reads_messaging_backend_value(self) -> None:
        cfg = _config_with({"foo": {"messaging_backend": "slack"}})
        with patch("teatree.config.load_config", return_value=cfg):
            assert toml_backends._toml_messaging_backend("foo") == "slack"

    def test_non_dict_entry_is_blank(self) -> None:
        cfg = _config_with({"foo": 7})
        with patch("teatree.config.load_config", return_value=cfg):
            assert toml_backends._toml_messaging_backend("foo") == ""
