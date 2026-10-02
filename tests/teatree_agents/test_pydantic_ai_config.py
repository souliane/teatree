"""The ``pydantic_ai`` capability constants advertise only what the lane actually enforces."""

import asyncio
import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import httpx2
import pytest
from django.test import TestCase
from pydantic_ai.models.test import TestModel

from teatree.agents import harness
from teatree.agents.pydantic_ai_config import (
    LANE_FACTORY,
    PYDANTIC_AI_NATIVE_CAPABILITIES,
    PYDANTIC_AI_ROUTER_CAPABILITIES,
    OpenAICompatibleLaneConfig,
    PydanticAiBinding,
    build_model_settings,
    build_openai_compatible_provider,
)
from teatree.agents.pydantic_ai_turn import SessionRun
from teatree.llm.usage_tee import UsageTee

_ORCAROUTER_HEADERS = {"X-OrcaRouter-Include-Cost": "true", "X-OrcaRouter-Session-Id": "t3-{session}"}
_CREDENTIAL_POINTER = "openai_compatible_credential_entry"
_UNLISTED_HEADERS: tuple[dict[str, str], ...] = (
    {"Authorization": "Bearer canary-value"},
    {"XAuthToken": "canary-value"},
    {" X-OrcaRouter-Session-Id": "t3-{session}"},
    {**_ORCAROUTER_HEADERS, "x-lane": "bulk"},
)
_COMPLETION = {
    "id": "c1",
    "object": "chat.completion",
    "created": 1,
    "model": "glm-5.3-flash",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 7, "completion_tokens": 1, "total_tokens": 8, "cost_usd": 0.000114},
}


class TestStructuredOutputCapabilityIsHonest:
    """The lane does NOT enforce a result schema, so neither binding may advertise it.

    Both capability constants claimed ``structured_output=True`` while nothing read the
    flag and the lane only scrapes the last JSON line of agent text. The flag is dead and
    the claim was misleading; both bindings report ``False``.
    """

    def test_router_binding_reports_no_structured_output(self) -> None:
        assert PYDANTIC_AI_ROUTER_CAPABILITIES.structured_output is False

    def test_native_binding_reports_no_structured_output(self) -> None:
        assert PYDANTIC_AI_NATIVE_CAPABILITIES.structured_output is False

    def test_no_agents_code_reads_the_structured_output_capability(self) -> None:
        # Grep-proof that the flag is dead: nothing in the agents package branches on
        # ``capabilities.structured_output`` (the eval judge's ``ResultMessage.structured_output``
        # lives in a different package and is a different concept). The capability is DEFINED
        # with ``structured_output=`` / ``structured_output:``, never read with ``.structured_output``.
        agents_dir = Path(harness.__file__).parent
        offenders = [
            f"{path.relative_to(agents_dir)}:{lineno}: {line.strip()}"
            for path in agents_dir.rglob("*.py")
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if ".structured_output" in line
        ]
        assert not offenders, f"the structured_output capability is dead — remove these reads: {offenders}"

    def test_config_source_only_mentions_structured_output_to_deny_it(self) -> None:
        # The docstrings were corrected to stop misleading the next reader: every mention of
        # the phrase must be negated ("no schema-enforced structured output"), never a positive
        # capability claim. Scans ALL occurrences, not just the first (the phrase recurs).
        from teatree.agents import pydantic_ai_config  # noqa: PLC0415 — reading the module's own source

        source = Path(pydantic_ai_config.__file__).read_text(encoding="utf-8")
        phrase = "schema-enforced structured output"
        index = source.find(phrase)
        assert index != -1, "expected the phrase to appear (negated) in the corrected docstrings"
        while index != -1:
            assert source[:index].rstrip().endswith("no"), (
                "the config source positively claims structured output — it must only deny it"
            )
            index = source.find(phrase, index + len(phrase))


class TestOpenAICompatibleProviderCarriesTheRunIdentityAndTheTee(TestCase):
    """The router sees one session per run and reports its cost, which the tee captures off the wire."""

    @pytest.fixture(autouse=True)
    def _backend_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", "https://api.example.invalid/v1")
        monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "dummy-backend-test-value")

    def _send_one_completion(
        self, config: OpenAICompatibleLaneConfig, tee: UsageTee, sent: list[httpx2.Request] | None = None
    ) -> httpx2.Request:
        provider = build_openai_compatible_provider(config, SessionRun(session_id="run-7", usage_tee=tee))
        sent = [] if sent is None else sent

        def network(request: httpx2.Request) -> httpx2.Response:
            sent.append(request)
            return httpx2.Response(200, json=_COMPLETION, headers={"X-Orca-Resolved-Model": "z-ai/glm-5.3-flash"})

        with patch.object(
            httpx2.AsyncHTTPTransport, "handle_async_request", httpx2.MockTransport(network).handle_async_request
        ):
            asyncio.run(
                provider.client.chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}])
            )
        [request] = sent
        return request

    def test_configured_headers_ride_every_request_with_the_session_substituted(self) -> None:
        request = self._send_one_completion(
            OpenAICompatibleLaneConfig(lane=LANE_FACTORY, extra_headers=_ORCAROUTER_HEADERS), UsageTee()
        )

        assert request.headers["X-OrcaRouter-Include-Cost"] == "true"
        assert request.headers["X-OrcaRouter-Session-Id"] == "t3-run-7"
        assert request.headers["x-lane"] == "factory"

    def test_an_unlisted_header_is_refused_before_it_can_reach_the_wire(self) -> None:
        sent: list[httpx2.Request] = []
        for headers in _UNLISTED_HEADERS:
            with self.subTest(headers=list(headers)), pytest.raises(ValueError, match=_CREDENTIAL_POINTER):
                self._send_one_completion(
                    OpenAICompatibleLaneConfig(lane=LANE_FACTORY, extra_headers=headers), UsageTee(), sent
                )
        assert sent == []

    def test_replacing_the_headers_on_a_config_is_refused_the_same_way(self) -> None:
        allowed = OpenAICompatibleLaneConfig(lane=LANE_FACTORY, extra_headers=_ORCAROUTER_HEADERS)
        with pytest.raises(ValueError, match=_CREDENTIAL_POINTER):
            dataclasses.replace(allowed, extra_headers={"XAuthToken": "canary-value"})

    def test_mutating_the_dict_passed_in_cannot_put_a_header_on_the_wire(self) -> None:
        passed_in = dict(_ORCAROUTER_HEADERS)
        config = OpenAICompatibleLaneConfig(lane=LANE_FACTORY, extra_headers=passed_in)
        passed_in.update({"Authorization": "Bearer canary-value", "X-Lane": "bulk"})

        request = self._send_one_completion(config, UsageTee())

        assert request.headers["Authorization"] == "Bearer dummy-backend-test-value"
        assert request.headers.get_list("x-lane") == ["factory"]
        assert request.headers["X-OrcaRouter-Session-Id"] == "t3-run-7"

    def test_the_exposed_headers_cannot_be_mutated(self) -> None:
        config = OpenAICompatibleLaneConfig(lane=LANE_FACTORY, extra_headers=dict(_ORCAROUTER_HEADERS))
        exposed = cast("dict[str, str]", config.extra_headers)
        for name in ("XAuthToken", "X-Lane"):
            with self.subTest(name=name), pytest.raises(TypeError):
                exposed[name] = "canary-value"

        request = self._send_one_completion(config, UsageTee())

        assert "XAuthToken" not in request.headers
        assert request.headers.get_list("x-lane") == ["factory"]
        assert request.headers["X-OrcaRouter-Include-Cost"] == "true"

    def test_the_provider_rechecks_the_allowlist_itself(self) -> None:
        unchecked = SimpleNamespace(
            lane=LANE_FACTORY,
            base_url="",
            model=None,
            credential_entry=None,
            extra_headers={**_ORCAROUTER_HEADERS, "XAuthToken": "canary-value", "X-Lane": "bulk"},
        )
        config = cast("OpenAICompatibleLaneConfig", unchecked)

        request = self._send_one_completion(config, UsageTee())

        assert "XAuthToken" not in request.headers
        assert request.headers.get_list("x-lane") == ["factory"]
        assert request.headers["X-OrcaRouter-Include-Cost"] == "true"

    def test_the_tee_on_the_provider_records_what_the_router_billed(self) -> None:
        tee = UsageTee()

        self._send_one_completion(OpenAICompatibleLaneConfig(lane=LANE_FACTORY), tee)

        [billed] = tee.requests
        assert billed.cost_usd == pytest.approx(0.000114)
        assert billed.as_record()["model"] == "z-ai/glm-5.3-flash"

    def test_no_configured_headers_sends_only_the_lane_header(self) -> None:
        request = self._send_one_completion(OpenAICompatibleLaneConfig(lane=LANE_FACTORY), UsageTee())

        assert "X-OrcaRouter-Session-Id" not in request.headers


class TestRouterModelSettingsCarryThePromptCacheKey:
    def test_the_router_binding_keys_the_provider_cache_on_the_session(self) -> None:
        settings = build_model_settings(
            TestModel(), None, binding=PydanticAiBinding.ROUTER, max_tokens=None, prompt_cache_key="run-7"
        )
        assert settings == {"openai_prompt_cache_key": "run-7"}

    def test_the_native_binding_never_receives_the_openai_cache_key(self) -> None:
        settings = build_model_settings(
            TestModel(), None, binding=PydanticAiBinding.NATIVE_ANTHROPIC, max_tokens=None, prompt_cache_key="run-7"
        )
        assert settings is None
