r"""The provider-neutral credential layer (``teatree.llm.credentials``).

This is THE canonical way to authenticate any Claude SDK / bundled-CLI invocation
in teatree. The credential resolves from an ordered list of injected
:class:`CredentialSource`\ s (env wins, then ``pass``), raises a loud
:class:`CredentialError` naming the fix when absent, and builds a child env that
sets its own env var and **strips** every conflicting credential — so a metered
invocation can never silently fall back to a different credential.

Dependency injection makes the whole surface unit-testable without touching the
real environment or the ``pass`` store: every test injects fake sources.
"""

import os
from collections.abc import Sequence
from unittest.mock import patch

import pytest

from teatree.llm.credentials import (
    AnthropicApiKeyCredential,
    AnthropicSubscriptionCredential,
    Credential,
    CredentialError,
    CredentialSource,
    CredentialSpec,
    EnvSource,
    PassSource,
)

_API_KEY_ENV = "ANTHROPIC_API_KEY"
_API_KEY_PASS = "anthropic/api-key"
_OAUTH_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
_OAUTH_PASS = "anthropic/oauth-token"


class _FakeEnvSource:
    """A :class:`CredentialSource` that reads ``spec.env_var`` from an in-memory dict."""

    def __init__(self, values: dict[str, str | None]) -> None:
        self._values = values

    def lookup(self, spec: CredentialSpec) -> str | None:
        return self._values.get(spec.env_var)


class _FakePassSource:
    """A :class:`CredentialSource` that reads ``spec.pass_path`` from an in-memory dict."""

    def __init__(self, values: dict[str, str | None]) -> None:
        self._values = values

    def lookup(self, spec: CredentialSpec) -> str | None:
        return self._values.get(spec.pass_path)


def _api_key_credential(sources: Sequence[CredentialSource]) -> AnthropicApiKeyCredential:
    return AnthropicApiKeyCredential(sources=sources)


class TestResolve:
    def test_env_source_wins_and_later_sources_are_not_consulted(self) -> None:
        consulted: list[str] = []

        class _RecordingSource:
            def lookup(self, spec: CredentialSpec) -> str | None:
                consulted.append(spec.pass_path)
                return None

        env = _FakeEnvSource({_API_KEY_ENV: "sk-env"})
        credential = _api_key_credential([env, _RecordingSource()])
        assert credential.resolve() == "sk-env"
        assert consulted == [], "the pass source must not be consulted once env resolves the key"

    def test_falls_through_to_the_pass_source_when_env_is_absent(self) -> None:
        env = _FakeEnvSource({_API_KEY_ENV: None})
        store = _FakePassSource({_API_KEY_PASS: "sk-pass"})
        credential = _api_key_credential([env, store])
        assert credential.resolve() == "sk-pass"

    def test_empty_value_is_treated_as_absent(self) -> None:
        env = _FakeEnvSource({_API_KEY_ENV: ""})
        store = _FakePassSource({_API_KEY_PASS: "sk-pass"})
        credential = _api_key_credential([env, store])
        assert credential.resolve() == "sk-pass", "an empty env value is not a real credential — fall through"

    def test_raises_loud_credential_error_with_fix_instructions_when_absent(self) -> None:
        credential = _api_key_credential([_FakeEnvSource({}), _FakePassSource({})])
        with pytest.raises(CredentialError) as excinfo:
            credential.resolve()
        message = str(excinfo.value)
        assert _API_KEY_ENV in message, "the error must name the env var the user can set"
        assert _API_KEY_PASS in message, "the error must name the pass entry the user can insert"
        assert "pass insert" in message, "the error must give the exact `pass insert` fix"
        assert "subscription" in message.lower(), (
            "the error must state metered evals never fall back to the subscription"
        )


class TestChildEnv:
    def test_api_key_credential_sets_its_var_and_strips_the_oauth_token(self) -> None:
        credential = _api_key_credential([_FakeEnvSource({_API_KEY_ENV: "sk-env"})])
        base = {"PATH": "/usr/bin", _OAUTH_ENV: "sub-token", "HOME": "/h"}
        child = credential.child_env(base)
        assert child[_API_KEY_ENV] == "sk-env"
        assert _OAUTH_ENV not in child, "the API-key child env must strip the conflicting OAuth token"
        assert child["PATH"] == "/usr/bin", "non-conflicting vars survive untouched"
        assert child["HOME"] == "/h"

    def test_child_env_does_not_mutate_the_base_mapping(self) -> None:
        credential = _api_key_credential([_FakeEnvSource({_API_KEY_ENV: "sk-env"})])
        base = {_OAUTH_ENV: "sub-token"}
        credential.child_env(base)
        assert base == {_OAUTH_ENV: "sub-token"}, "child_env must return a copy, never mutate the caller's base"

    def test_subscription_credential_sets_its_var_and_strips_the_api_key(self) -> None:
        credential = AnthropicSubscriptionCredential(sources=[_FakeEnvSource({_OAUTH_ENV: "sub-token"})])
        base = {"PATH": "/usr/bin", _API_KEY_ENV: "sk-env"}
        child = credential.child_env(base)
        assert child[_OAUTH_ENV] == "sub-token"
        assert _API_KEY_ENV not in child, "the subscription child env must strip the conflicting API key"
        assert child["PATH"] == "/usr/bin"

    def test_child_env_raises_loud_when_no_credential_is_resolvable(self) -> None:
        credential = _api_key_credential([_FakeEnvSource({})])
        with pytest.raises(CredentialError):
            credential.child_env({"PATH": "/usr/bin"})


class TestConcreteSpecs:
    def test_api_key_spec_is_provider_explicit(self) -> None:
        spec = AnthropicApiKeyCredential().spec
        assert spec.env_var == _API_KEY_ENV
        assert spec.pass_path == _API_KEY_PASS
        assert spec.conflicting_vars == (_OAUTH_ENV,)

    def test_subscription_spec_is_the_inverse(self) -> None:
        spec = AnthropicSubscriptionCredential().spec
        assert spec.env_var == _OAUTH_ENV
        assert spec.pass_path == _OAUTH_PASS
        assert spec.conflicting_vars == (_API_KEY_ENV,)

    def test_credential_spec_is_frozen(self) -> None:
        spec = CredentialSpec(env_var="X", pass_path="a/x", conflicting_vars=())
        with pytest.raises(AttributeError):
            spec.env_var = "Y"  # type: ignore[misc]


class TestDefaultSources:
    def test_default_sources_are_env_then_pass(self) -> None:
        # The default wiring (no injected sources) is env → pass, the production order.
        credential = AnthropicApiKeyCredential()
        kinds = [type(source) for source in credential.sources]
        assert kinds == [EnvSource, PassSource]

    def test_env_source_reads_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        spec = CredentialSpec(env_var="SOME_TEST_VAR", pass_path="some/test", conflicting_vars=())
        monkeypatch.setenv("SOME_TEST_VAR", "v")
        assert EnvSource().lookup(spec) == "v"
        monkeypatch.delenv("SOME_TEST_VAR", raising=False)
        assert EnvSource().lookup(spec) is None

    def test_pass_source_returns_none_for_a_missing_entry(self) -> None:
        # A missing `pass` entry must read as absent (None), never crash — the
        # anthropic/api-key entry does not exist yet.
        spec = AnthropicApiKeyCredential().spec
        with patch("teatree.llm.credentials.read_pass", return_value=""):
            assert PassSource().lookup(spec) is None

    def test_pass_source_returns_the_value_for_a_present_entry(self) -> None:
        spec = AnthropicApiKeyCredential().spec
        with patch("teatree.llm.credentials.read_pass", return_value="sk-stored"):
            assert PassSource().lookup(spec) == "sk-stored"


class TestExport:
    def test_export_resolves_and_writes_the_value_into_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # export() is the in-process side effect a docker `-e VARNAME` pass-through
        # relies on: the key must land in os.environ so the passthrough forwards it.
        monkeypatch.delenv(_API_KEY_ENV, raising=False)
        credential = _api_key_credential([_FakePassSource({_API_KEY_PASS: "sk-pass"})])
        returned = credential.export()
        assert returned == "sk-pass"
        assert os.environ.get(_API_KEY_ENV) == "sk-pass", "export() must set the value in os.environ"

    def test_export_raises_loud_when_no_credential_is_resolvable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(_API_KEY_ENV, raising=False)
        credential = _api_key_credential([_FakeEnvSource({}), _FakePassSource({})])
        with pytest.raises(CredentialError):
            credential.export()


class TestBaseCredentialContract:
    def test_credential_is_the_shared_base(self) -> None:
        assert issubclass(AnthropicApiKeyCredential, Credential)
        assert issubclass(AnthropicSubscriptionCredential, Credential)
