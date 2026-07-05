"""Router-side mirror wiring: the poster builder + the #2171 audio enricher.

``build_dm_audio_enricher`` gates the mirror's audio arm on ``speak.slack`` plus
the ``say`` / ``t3`` binaries; ``dispatch_dm_audio`` spawns ``t3 speak-dm``
DETACHED so synthesis never blocks the ~5 s hook budget. When ``speak.slack`` is
off the enricher is ``None`` — the mirror stays text-only, exactly as before
#2171. The router wires them into ``_perform_slack_post`` off the cold-read
``speak.slack``. Only the PATH lookup and the detached subprocess are faked.
"""

from unittest.mock import MagicMock, patch

import hooks.scripts.hook_router as router
import hooks.scripts.slack_mirror_wiring as wiring


class TestBuildEnricher:
    def test_none_when_slack_off(self) -> None:
        with patch.object(wiring.shutil, "which", return_value="/usr/bin/x"):
            assert wiring.build_dm_audio_enricher(slack_enabled=False) is None

    def test_none_when_say_absent(self) -> None:
        with patch.object(wiring.shutil, "which", return_value=None):
            assert wiring.build_dm_audio_enricher(slack_enabled=True) is None

    def test_callable_when_slack_on_and_binaries_present(self) -> None:
        with patch.object(wiring.shutil, "which", return_value="/usr/bin/x"):
            assert wiring.build_dm_audio_enricher(slack_enabled=True) is wiring.dispatch_dm_audio


class TestDispatchDmAudio:
    def test_spawns_detached_speak_dm(self, monkeypatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        with (
            patch.object(wiring.shutil, "which", return_value="/usr/local/bin/t3"),
            patch.object(wiring.subprocess, "Popen") as popen,
        ):
            wiring.dispatch_dm_audio("D-USER", "Ship it?", "1700.1")
        argv = popen.call_args.args[0]
        assert argv[:2] == ["/usr/local/bin/t3", "speak-dm"]
        assert argv[argv.index("--channel") + 1] == "D-USER"
        assert argv[argv.index("--text") + 1] == "Ship it?"
        assert argv[argv.index("--thread-ts") + 1] == "1700.1"
        assert popen.call_args.kwargs["start_new_session"] is True

    def test_includes_overlay_when_set(self, monkeypatch) -> None:
        monkeypatch.setenv("T3_OVERLAY_NAME", "acme")
        with (
            patch.object(wiring.shutil, "which", return_value="/usr/local/bin/t3"),
            patch.object(wiring.subprocess, "Popen") as popen,
        ):
            wiring.dispatch_dm_audio("D", "hi", "")
        argv = popen.call_args.args[0]
        assert argv[argv.index("--overlay") + 1] == "acme"
        assert "--thread-ts" not in argv  # omitted when empty

    def test_noop_without_t3_binary(self) -> None:
        with (
            patch.object(wiring.shutil, "which", return_value=None),
            patch.object(wiring.subprocess, "Popen") as popen,
        ):
            wiring.dispatch_dm_audio("D", "hi", "")
        popen.assert_not_called()

    def test_popen_failure_is_swallowed(self) -> None:
        with (
            patch.object(wiring.shutil, "which", return_value="/usr/local/bin/t3"),
            patch.object(wiring.subprocess, "Popen", side_effect=OSError("no fork")),
        ):
            wiring.dispatch_dm_audio("D", "hi", "")  # must not raise


class TestSlackHttpPoster:
    def test_builds_no_retry_hook_budget_client(self) -> None:
        client = MagicMock()
        with patch("teatree.backends.slack.http.SlackHttpClient", return_value=client) as cls:
            poster = wiring.slack_http_poster()
        cls.assert_called_once_with(timeout=wiring._SLACK_POST_TIMEOUT_SECONDS, max_retries=0)
        assert poster is client.post


class TestRouterWiring:
    """``_perform_slack_post`` reads ``speak.slack`` and forwards the built enricher to the leaf."""

    def test_forwards_cold_read_slack_and_built_enricher(self) -> None:
        captured: dict[str, object] = {}
        sentinel = object()

        def _fake_perform(*_a: object, **kwargs: object) -> str:
            captured.update(kwargs)
            return "1700.1"

        builder = MagicMock(return_value=sentinel)
        with (
            patch.object(router, "_speak_settings", return_value=("off", True)),
            patch.object(router, "build_dm_audio_enricher", builder),
            patch("teatree.hooks.slack_mirror.perform_slack_post", side_effect=_fake_perform),
            patch.object(router, "_slack_http_poster", return_value=object()),
        ):
            router._perform_slack_post(("ref", "U1"), [{"question": "Ship?"}])
        builder.assert_called_once_with(slack_enabled=True)  # the cold-read speak.slack bool
        assert captured["enrich_audio"] is sentinel
