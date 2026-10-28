"""The documented speech surfaces match the reachable DM delivery paths."""

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def test_speech_docs_describe_dm_audio_without_attended_turn_read_aloud() -> None:
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    blueprint = (_ROOT / "BLUEPRINT.md").read_text(encoding="utf-8")
    config = (_ROOT / "docs/blueprint/configuration.md").read_text(encoding="utf-8")
    assert "deliver_user_dm" in blueprint
    assert "deliver_user_dm_sidecar" in config
    assert "notify_user" in config
    assert "SlackBotBackend.post_audio_dm" in config
    assert "t3 speak" not in readme + blueprint + config
    assert "Stop-hook reading" not in config
    assert "`off`/`dm`/`all`" not in blueprint + config
