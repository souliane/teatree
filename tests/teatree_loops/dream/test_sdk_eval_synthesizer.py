"""Tests for the real LLM eval-synthesizer SEAM — tier-resolved, watchdog-bounded (#2447, §3a #1).

The synthesizer's defensive reply parsing + prompt grammar are exercised in
``test_llm_eval_proposer.py``; this mirror file pins the turn-shape concerns this seam
owns: the tier-resolved (not hardcoded) model, the model-agnostic plain-string system
prompt, and the WHOLE-turn ``asyncio.timeout`` watchdog that bounds a stalled ``claude``
connect (the prior watchdog wrapped only the response drain, so a stall hung forever).
"""

import asyncio
import json
import os
import sqlite3
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Self
from unittest.mock import patch

import claude_agent_sdk
import pytest
from django.test import SimpleTestCase, TestCase

from teatree.agents.model_tiering import resolve_tier
from teatree.core.models import ConfigSetting
from teatree.llm.credentials import CredentialError
from teatree.loops.dream import sdk_eval_synthesizer
from tests.teatree_agents._sdk_fake import FakeHarnessSession, assistant_text

_CANDIDATE: dict[str, object] = {"scenario_name": "x_under_load", "drift_rule": "d", "seed_citation": "c"}
_SLICE = "a session slice"


def _recording_client(reply: str) -> Callable[..., FakeHarnessSession]:
    """A fake ``ClaudeSDKClient`` that records the ``options`` it was opened with."""

    def _make_client(*, options: object = None, **_: object) -> FakeHarnessSession:
        FakeHarnessSession.last_options = options
        return FakeHarnessSession([assistant_text(reply)])

    return _make_client


def _seed_config_setting(db_path: Path, key: str, raw_value: str) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS teatree_config_setting (id INTEGER PRIMARY KEY, scope TEXT, key TEXT, value TEXT)"
    )
    conn.execute("INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', ?, ?)", (key, raw_value))
    conn.commit()
    conn.close()


class SynthOptionsTierResolutionTestCase(SimpleTestCase):
    """The synthesizer turn is tier-resolved with a plain-string prompt, not a hardcoded id."""

    def test_model_defaults_to_the_cheap_tier(self) -> None:
        options = sdk_eval_synthesizer._synth_options()
        assert options.model == resolve_tier("cheap")

    def test_model_follows_the_cheap_tier_db_override(self) -> None:
        # RED before the fix: the model was the hardcoded ``_SYNTH_MODEL``, so an
        # ``agent_tier_models`` DB override for the cheap tier was silently ignored.
        db = Path(self.enterContext(tempfile.TemporaryDirectory())) / "config.sqlite3"
        _seed_config_setting(db, "agent_tier_models", json.dumps({"cheap": "vendor/custom-cheap"}))
        with patch.dict("os.environ", {"T3_CONFIG_DB": str(db)}):
            options = sdk_eval_synthesizer._synth_options()
        assert options.model == "vendor/custom-cheap"

    def test_system_prompt_is_a_plain_string(self) -> None:
        options = sdk_eval_synthesizer._synth_options()
        assert isinstance(options.system_prompt, str)
        assert "eval" in options.system_prompt.lower()


def _no_credentials() -> dict[str, str] | None:
    """#3512: the SimpleTestCase turns inject the credential resolver, never read the DB."""
    return None


class SdkSynthesizerGuardTestCase(SimpleTestCase):
    def test_missing_claude_binary_raises(self) -> None:
        with (
            patch("shutil.which", return_value=None),
            pytest.raises(RuntimeError, match="claude is not installed"),
        ):
            sdk_eval_synthesizer.sdk_spec_synthesizer(_CANDIDATE, _SLICE, child_env=_no_credentials)

    def test_a_malformed_reply_raises_so_the_candidate_is_dropped(self) -> None:
        # A reply carrying no JSON object must RAISE, so the caller drops the candidate
        # rather than staging an unproven spec from a fake success.
        async def _reply(_prompt: str, *, env: dict[str, str] | None = None) -> str:
            await asyncio.sleep(0)
            return "not a JSON object at all"

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch.object(sdk_eval_synthesizer, "_collect_synth_turn", _reply),
            pytest.raises(ValueError, match="no JSON object"),
        ):
            sdk_eval_synthesizer.sdk_spec_synthesizer(_CANDIDATE, _SLICE, child_env=_no_credentials)


class _HangOnConnectClient:
    """A ``ClaudeSDKClient`` stand-in whose connect (``__aenter__``) never returns.

    Models a ``claude`` subprocess that stalls during spawn/handshake — the region
    the prior drain-only watchdog did NOT cover, so a real stall there hung the
    derivation forever.
    """

    def __init__(self, *, options: object = None, **_: object) -> None:
        self._options = options

    async def __aenter__(self) -> Self:
        await asyncio.sleep(30)  # connect stalls; only the whole-turn watchdog can bound it
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def query(self, prompt: str) -> None:  # pragma: no cover - connect hangs first
        return None

    async def receive_response(self) -> object:  # pragma: no cover - connect hangs first
        return
        yield  # unreachable


class SdkSynthesizerWatchdogTestCase(SimpleTestCase):
    def test_turn_is_time_bounded_when_sdk_connect_hangs(self) -> None:
        # Anti-vacuous regression pin: a stalled ``claude`` CONNECT must raise
        # TimeoutError within the whole-turn watchdog, never hang forever. RED on the
        # pre-fix drain-only watchdog; GREEN once ``asyncio.timeout`` bounds the whole
        # ``async with``. Run on a thread so a regression hangs the THREAD, not the suite.
        captured: dict[str, BaseException | None] = {}

        def _run() -> None:
            try:
                sdk_eval_synthesizer.sdk_spec_synthesizer(_CANDIDATE, _SLICE, child_env=_no_credentials)
                captured["exc"] = None
            except BaseException as exc:  # noqa: BLE001 - record whatever the turn raised
                captured["exc"] = exc

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch.object(sdk_eval_synthesizer, "_SYNTH_WATCHDOG_SECONDS", 0.5),
            patch.object(claude_agent_sdk, "ClaudeSDKClient", _HangOnConnectClient),
        ):
            thread = threading.Thread(target=_run, daemon=True)
            thread.start()
            thread.join(timeout=8)

        assert not thread.is_alive(), (
            "synthesizer SDK turn was NOT time-bounded: a stalled claude connect hangs the derivation forever"
        )
        assert isinstance(captured.get("exc"), TimeoutError), (
            f"expected the watchdog to raise TimeoutError on a stalled turn, got {captured.get('exc')!r}"
        )


class SdkSynthesizerCredentialEnvTestCase(TestCase):
    """The synthesizer authenticates its ``claude`` subprocess via the configured provider.

    Same regression class as the distiller: a system ``claude`` turn spawned with no
    credential env fails to authenticate and its reply parses as malformed, silently
    dropping every derived candidate. The reply parse is exercised elsewhere, so these
    isolate the credential-threading concern. DB access: reads ``agent_harness_provider``.
    """

    def test_options_env_pins_subscription_token(self) -> None:
        # RED before the fix: _synth_options set no env, so options.env == {}.
        ConfigSetting.objects.set_value("agent_harness_provider", "subscription_oauth")
        with (
            patch.object(sdk_eval_synthesizer, "_parse_synthesized", return_value=object()),
            patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "oauth-x", "GIT_DIR": "/outer/.git"}),
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch.object(claude_agent_sdk, "ClaudeSDKClient", _recording_client("{}")),
        ):
            sdk_eval_synthesizer.sdk_spec_synthesizer(_CANDIDATE, _SLICE)

        options = FakeHarnessSession.last_options
        assert options is not None
        assert options.env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-x"
        assert "ANTHROPIC_API_KEY" not in options.env
        assert "GIT_DIR" not in options.env

    def test_unresolvable_credential_fails_loud_before_any_turn(self) -> None:
        # An auth gap RAISES before the turn spawns, so it can never masquerade as a
        # malformed reply that silently drops the candidate. The client is never built.
        ConfigSetting.objects.set_value("agent_harness_provider", "subscription_oauth")

        def _never(*_a: object, **_k: object) -> FakeHarnessSession:
            msg = "the claude turn must not start when the credential is unresolvable"
            raise AssertionError(msg)

        with (
            patch(
                "teatree.agents._headless_env.resolve_subscription_credential",
                side_effect=CredentialError("no subscription token resolvable"),
            ),
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch.object(claude_agent_sdk, "ClaudeSDKClient", _never),
            pytest.raises(CredentialError),
        ):
            sdk_eval_synthesizer.sdk_spec_synthesizer(_CANDIDATE, _SLICE)
