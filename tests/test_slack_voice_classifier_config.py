"""TOML config resolution for ``slack_voice_classifier_mode`` (#1395).

Confirms the loader accepts both the flat ``[teatree]
slack_voice_classifier_mode`` key and the nested
``[teatree.publish_gates]`` table the issue brief sketched, defaults
to ``WARN`` when absent (backward-compat), and surfaces a clean
``ValueError`` on a typo so a silent mode downgrade never lands.
"""

from pathlib import Path

import pytest

from teatree.backends.slack.voice_classifier import ClassifierMode
from teatree.config import load_config


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / ".teatree.toml"
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)
    return cfg


def _write(cfg: Path, body: str) -> None:
    cfg.write_text(body, encoding="utf-8")


class TestVoiceClassifierModeResolution:
    def test_default_is_warn_when_absent(self, config_file: Path) -> None:
        _write(config_file, "[teatree]\n")
        assert load_config().user.slack_voice_classifier_mode is ClassifierMode.WARN

    def test_default_is_warn_when_file_missing(self, config_file: Path) -> None:
        # No file written; loader returns defaults.
        assert load_config().user.slack_voice_classifier_mode is ClassifierMode.WARN

    def test_flat_strict(self, config_file: Path) -> None:
        _write(config_file, '[teatree]\nslack_voice_classifier_mode = "strict"\n')
        assert load_config().user.slack_voice_classifier_mode is ClassifierMode.STRICT

    def test_flat_off(self, config_file: Path) -> None:
        _write(config_file, '[teatree]\nslack_voice_classifier_mode = "off"\n')
        assert load_config().user.slack_voice_classifier_mode is ClassifierMode.OFF

    def test_nested_publish_gates_strict(self, config_file: Path) -> None:
        _write(
            config_file,
            '[teatree.publish_gates]\nslack_voice_classifier_mode = "strict"\n',
        )
        assert load_config().user.slack_voice_classifier_mode is ClassifierMode.STRICT

    def test_flat_wins_over_nested(self, config_file: Path) -> None:
        _write(
            config_file,
            (
                "[teatree]\n"
                'slack_voice_classifier_mode = "off"\n'
                "[teatree.publish_gates]\n"
                'slack_voice_classifier_mode = "strict"\n'
            ),
        )
        assert load_config().user.slack_voice_classifier_mode is ClassifierMode.OFF

    def test_invalid_value_raises(self, config_file: Path) -> None:
        _write(config_file, '[teatree]\nslack_voice_classifier_mode = "loud"\n')
        with pytest.raises(ValueError, match="slack_voice_classifier_mode"):
            load_config()
