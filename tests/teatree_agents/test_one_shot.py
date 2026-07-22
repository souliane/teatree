"""The shared ``run_one_shot`` seam — tier-resolved, harness-routed, clean-room (§3a #1).

``run_one_shot`` collapses the aux one-shot call sites (Slack ``simple_answer``,
``ticket_short_describe``) onto ONE helper that resolves the abstract tier to a
concrete model id and drives the turn through the provider-agnostic harness seam.
These tests drive it end-to-end through the REAL cold-config DB (a tier override
reaches the built options) and through a REAL ``PydanticAiHarness`` under a
pydantic_ai test double (zero tokens), plus the clean-room + failure contract.
"""

import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from pydantic_ai.models.test import TestModel

from teatree.agents.harness import PydanticAiHarness
from teatree.agents.one_shot import OneShotSpec, _clean_room_options, run_one_shot
from teatree.llm.credentials import CredentialError
from tests.teatree_agents._sdk_fake import FakeHarness, assistant_text, result_message


def _seed(db_path: Path, key: str, value: object) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS teatree_config_setting (id INTEGER PRIMARY KEY, scope TEXT, key TEXT, value TEXT)"
    )
    conn.execute("INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', ?, ?)", (key, json.dumps(value)))
    conn.commit()
    conn.close()


class _RaisingHarness:
    """A ``Harness`` double whose ``open`` fails — exercises the degrade-to-None contract."""

    @asynccontextmanager
    async def open(self, _options: ClaudeAgentOptions) -> AsyncIterator[object]:
        msg = "backend unavailable"
        raise RuntimeError(msg)
        yield  # pragma: no cover — unreachable, marks this an async generator


class TestRunOneShotTierResolution:
    def test_tier_override_from_the_real_cold_db_reaches_the_built_options(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # End-to-end through the real cold-config DB: an ``agent_tier_models``
        # row for the cheap tier is what the harness is opened with — proving the
        # turn follows a swapped tier-model, not a hardcoded id.
        db = tmp_path / "config.sqlite3"
        _seed(db, "agent_tier_models", {"cheap": "custom-cheap-x"})
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        harness = FakeHarness([assistant_text("hi"), result_message()])

        result = run_one_shot("q", OneShotSpec(system_prompt="be terse"), harness=harness)

        assert result == "hi"
        assert harness.opened_options.model == "custom-cheap-x"

    def test_default_tier_resolves_to_the_shipped_cheap_model(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from teatree.agents.model_tiering import TIER_MODELS  # noqa: PLC0415 — deferred: test-local

        monkeypatch.setenv("T3_CONFIG_DB", str(tmp_path / "absent.sqlite3"))
        harness = FakeHarness([assistant_text("ok"), result_message()])
        run_one_shot("q", OneShotSpec(system_prompt="p"), harness=harness)
        assert harness.opened_options.model == TIER_MODELS["cheap"]


class TestRunOneShotCleanRoom:
    def test_clean_room_options_are_pinned(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_CONFIG_DB", str(tmp_path / "absent.sqlite3"))
        harness = FakeHarness([assistant_text("ok"), result_message()])
        run_one_shot("q", OneShotSpec(system_prompt="whole system prompt", max_turns=1), harness=harness)
        options = harness.opened_options
        # No personal-context bias, no tools, a single stateless turn, and the
        # spec's whole system prompt (never a preset).
        assert options.setting_sources == []
        assert options.tools == []
        assert options.max_turns == 1
        assert options.system_prompt == "whole system prompt"


class TestRunOneShotFailure:
    def test_backend_failure_degrades_to_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_CONFIG_DB", str(tmp_path / "absent.sqlite3"))
        assert run_one_shot("q", OneShotSpec(system_prompt="p"), harness=_RaisingHarness()) is None

    def test_empty_text_degrades_to_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_CONFIG_DB", str(tmp_path / "absent.sqlite3"))
        harness = FakeHarness([assistant_text("   "), result_message()])
        assert run_one_shot("q", OneShotSpec(system_prompt="p"), harness=harness) is None


class TestRunOneShotPydanticAiBackend:
    """The turn drives a REAL ``PydanticAiHarness`` off-Claude — proved with a pydantic_ai double."""

    def test_pydantic_ai_backend_drives_the_turn_zero_tokens(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A REAL PydanticAiHarness (real pydantic_ai Agent + TestModel, no network,
        # no token) — the same seam ``resolve_harness`` returns for
        # ``agent_harness=pydantic_ai`` — driven end-to-end through run_one_shot.
        monkeypatch.setenv("T3_CONFIG_DB", str(tmp_path / "absent.sqlite3"))
        harness = PydanticAiHarness(model=TestModel(custom_output_text="answer from pydantic_ai"))
        result = run_one_shot("q", OneShotSpec(system_prompt="p"), harness=harness)
        assert result == "answer from pydantic_ai"


class TestOneShotRefusesBaseUrlRedirect:
    """A clean-room turn pins no credential, so it inherits an ambient redirect.

    ``run_one_shot`` swallows every exception to degrade quietly, so the guard runs in
    ``_clean_room_options`` — OUTSIDE that try — or a misconfigured turn would silently
    return ``None`` while having been redirected. Its callers post on the user's behalf
    (``simple_answer``) and describe tickets, so a silent redirect is the worst shape.
    """

    def test_the_refusal_escapes_rather_than_degrading_to_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.example.invalid/v1")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-x")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(CredentialError) as excinfo:
            run_one_shot("hi", OneShotSpec(system_prompt="s"))
        assert "ANTHROPIC_BASE_URL" in str(excinfo.value)

    def test_a_metered_key_at_a_gateway_is_allowed_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.example.invalid/v1")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-key")
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        _clean_room_options(OneShotSpec(system_prompt="s"))


class TestOneShotPydanticLaneCredentialRefusalRaises:
    """A credential the ``pydantic_ai`` lane resolves LAZILY inside ``open`` RAISES, never None.

    Unlike the ambient base-URL guard (checked before ``run_one_shot``'s try), this refusal
    surfaces from INSIDE the degrade-to-None try, so it must be re-raised past the blanket
    handler — a missing metered key is an operator misconfiguration to surface, not a silent
    None that strands every caller on its fallback with nothing naming the cause.
    """

    def test_refused_lazy_credential_raises_credential_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("T3_CONFIG_DB", str(tmp_path / "absent.sqlite3"))
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        monkeypatch.setenv("ORCA_ROUTER_BASE_URL", "https://router.example.invalid/v1")
        monkeypatch.delenv("ORCA_ROUTER_API_KEY", raising=False)
        # model=None forces the REAL lazy OrcaRouter credential resolution inside open() — no
        # network is reached, the credential refusal fires before the client is built.
        harness = PydanticAiHarness(model=None)
        with pytest.raises(CredentialError):
            run_one_shot("q", OneShotSpec(system_prompt="p"), harness=harness)

    def test_a_non_credential_backend_error_still_degrades_to_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The re-raise is narrow: every OTHER backend failure still collapses to None (the claude
        # lane never raises CredentialError from open()), so a best-effort aux turn never breaks.
        monkeypatch.setenv("T3_CONFIG_DB", str(tmp_path / "absent.sqlite3"))
        assert run_one_shot("q", OneShotSpec(system_prompt="p"), harness=_RaisingHarness()) is None
