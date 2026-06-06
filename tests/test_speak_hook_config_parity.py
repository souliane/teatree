"""Hook-side ``_speak_settings`` and config-side ``resolve_speak`` agree (#2050).

The Stop hook reads ``[teatree.speak]`` directly from toml (it cannot
cheaply import the Django config), so it carries a small pure-Python
duplicate of the sub-table precedence. This golden-corpus parity test
pins the two implementations to the same ``(local, slack_audio, scope)``
for every toml shape, so the duplicate can never drift
(architecture-design check 8).
"""

from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from teatree.config_speak import resolve_speak

_CORPUS: list[str] = [
    '[teatree.speak]\nlocal = true\nslack_audio = true\nscope = "all"\n',
    "[teatree.speak]\nslack_audio = true\n",
    '[teatree.speak]\nlocal = true\nscope = "all"\n',
    '[teatree.speak]\nscope = "dm"\n',
    "[teatree.speak]\n",
    "[teatree]\n",
    "[other]\nx = 1\n",
]


@pytest.mark.parametrize("toml_body", _CORPUS)
def test_hook_and_config_sub_table_parity(toml_body: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text(toml_body, encoding="utf-8")
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)
    monkeypatch.setattr(router.Path, "home", classmethod(lambda _cls: tmp_path))

    from teatree.config import load_config  # noqa: PLC0415

    config_value = load_config().user.speak
    hook_local, hook_slack_audio, hook_scope = router._speak_settings()

    assert (hook_local, hook_slack_audio, hook_scope) == (
        config_value.local,
        config_value.slack_audio,
        config_value.scope.value,
    )


def test_resolve_speak_is_the_config_source_of_truth() -> None:
    # The hook map mirrors resolve_speak; pin that the config helper exists
    # and reads the new sub-table the hook duplicates.
    assert resolve_speak({"speak": {"local": True, "scope": "all"}}).scope.value == "all"
