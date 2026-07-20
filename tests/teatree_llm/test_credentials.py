r"""The provider-neutral credential layer (``teatree.llm.credentials``).

This is THE canonical way to authenticate any Claude SDK / bundled-CLI invocation
in teatree. The credential resolves from an ordered list of injected
:class:`CredentialSource`\ s (env wins, then ``pass``), raises a loud
:class:`CredentialError` naming the fix when absent, and builds a child env that
sets its own env var and **strips** every conflicting credential — so a metered
invocation can never silently fall back to a different credential.

The module is FOUNDATION-pure: it never reads the config store. A per-account
``pass_path`` override is INJECTED as a plain string (``pass_path_override``); the
domain-layer factory ``teatree.credential_config`` resolves it from
``ConfigSetting`` and passes it in. These tests therefore need no DB — they inject
the override directly. Dependency injection makes the whole surface unit-testable
without touching the real environment or the ``pass`` store: every test injects
fake sources.
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
    OrcaRouterCredential,
    PassSource,
    resolve_orca_router_provider_config,
)

_API_KEY_ENV = "ANTHROPIC_API_KEY"
_API_KEY_PASS = "anthropic/api-key"
_API_KEY_SETTING = "anthropic_api_key_pass_paths"
_OAUTH_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
_OAUTH_SETTING = "anthropic_oauth_pass_paths"
_ORCA_ENV = "ORCA_ROUTER_API_KEY"
_ORCA_PASS = "orcarouter/routed-account/api-key"
_ORCA_SETTING = "orca_router_pass_path"
_ORCA_BASE_URL_ENV = "ORCA_ROUTER_BASE_URL"


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


def _api_key_credential(
    sources: Sequence[CredentialSource], *, pass_path_override: str | None = None
) -> AnthropicApiKeyCredential:
    return AnthropicApiKeyCredential(sources=sources, pass_path_override=pass_path_override)


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
        # The API-key credential has no built-in default, so pass resolution needs a
        # configured (injected) per-account path.
        env = _FakeEnvSource({_API_KEY_ENV: None})
        store = _FakePassSource({_API_KEY_PASS: "sk-pass"})
        credential = _api_key_credential([env, store], pass_path_override=_API_KEY_PASS)
        assert credential.resolve() == "sk-pass"

    def test_empty_value_is_treated_as_absent(self) -> None:
        env = _FakeEnvSource({_API_KEY_ENV: ""})
        store = _FakePassSource({_API_KEY_PASS: "sk-pass"})
        credential = _api_key_credential([env, store], pass_path_override=_API_KEY_PASS)
        assert credential.resolve() == "sk-pass", "an empty env value is not a real credential — fall through"

    def test_raises_loud_credential_error_with_fix_instructions_when_absent(self) -> None:
        credential = _api_key_credential([_FakeEnvSource({}), _FakePassSource({})])
        with pytest.raises(CredentialError) as excinfo:
            credential.resolve()
        message = str(excinfo.value)
        assert _API_KEY_ENV in message, "the error must name the env var the user can set"
        assert _API_KEY_SETTING in message, "the error must name the routing setting to configure"
        assert _OAUTH_ENV in message, "the error must name the conflicting credential this one never falls back to"


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
    def test_api_key_spec_has_no_default_pass_path(self) -> None:
        spec = AnthropicApiKeyCredential().spec
        assert spec.env_var == _API_KEY_ENV
        assert spec.pass_path is None
        assert spec.routing_setting == _API_KEY_SETTING
        assert spec.conflicting_vars == (_OAUTH_ENV,)

    def test_subscription_spec_has_no_default_pass_path(self) -> None:
        # The subscription credential is the near-inverse of the API key, but with NO
        # built-in default `pass` path: it resolves only from env or configured routing.
        spec = AnthropicSubscriptionCredential().spec
        assert spec.env_var == _OAUTH_ENV
        assert spec.pass_path is None
        assert spec.routing_setting == _OAUTH_SETTING
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
        # A missing `pass` entry must read as absent (None), never crash.
        spec = CredentialSpec(env_var=_API_KEY_ENV, conflicting_vars=(), pass_path=_API_KEY_PASS)
        with patch("teatree.llm.credentials.read_pass", return_value=""):
            assert PassSource().lookup(spec) is None

    def test_pass_source_returns_the_value_for_a_present_entry(self) -> None:
        spec = CredentialSpec(env_var=_API_KEY_ENV, conflicting_vars=(), pass_path=_API_KEY_PASS)
        with patch("teatree.llm.credentials.read_pass", return_value="sk-stored"):
            assert PassSource().lookup(spec) == "sk-stored"


class TestExport:
    def test_export_resolves_and_writes_the_value_into_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # export() is the in-process side effect a docker `-e VARNAME` pass-through
        # relies on: the key must land in os.environ so the passthrough forwards it.
        # Seed the var THROUGH monkeypatch (not a bare delenv, which records no undo
        # for an already-absent key) so teardown reliably strips what export() writes
        # straight into os.environ — otherwise the value leaks into every later
        # test's environment, a cross-shard order-dependent flake. The credential
        # resolves from the injected store, not env, so the seeded value is inert.
        monkeypatch.setenv(_API_KEY_ENV, "")
        store = _FakePassSource({_API_KEY_PASS: "sk-pass"})
        credential = _api_key_credential([store], pass_path_override=_API_KEY_PASS)
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


# (credential class, env var) — no credential carries a built-in default `pass` path,
# so the injected-override behaviour below is parametrized over both Anthropic rules.
_ROUTED_PASS = "anthropic/routed-account/entry"
_OVERRIDE_CREDENTIALS = [
    pytest.param(AnthropicSubscriptionCredential, _OAUTH_ENV, id="subscription"),
    pytest.param(AnthropicApiKeyCredential, _API_KEY_ENV, id="metered"),
]
_credential_case = pytest.mark.parametrize(("credential_cls", "env_var"), _OVERRIDE_CREDENTIALS)


class TestPassPathOverride:
    """The ``pass_path`` a credential resolves against is set via an injected override string."""

    @_credential_case
    def test_injected_override_redirects_the_pass_path(self, credential_cls: type[Credential], env_var: str) -> None:
        store = _FakePassSource({_ROUTED_PASS: "routed"})
        assert credential_cls(sources=[store], pass_path_override=_ROUTED_PASS).resolve() == "routed"

    @_credential_case
    def test_env_still_wins_over_the_pass_override(self, credential_cls: type[Credential], env_var: str) -> None:
        # The override only moves where PassSource reads; EnvSource precedence is unchanged.
        sources = [_FakeEnvSource({env_var: "from-env"}), _FakePassSource({_ROUTED_PASS: "routed"})]
        assert credential_cls(sources=sources, pass_path_override=_ROUTED_PASS).resolve() == "from-env"

    @_credential_case
    def test_override_leaves_static_spec_env_var_and_conflicts_unchanged(
        self, credential_cls: type[Credential], env_var: str
    ) -> None:
        # The static-spec consumers (docker.py .spec.env_var, isolation.py
        # .spec.conflicting_vars) must keep working: only pass_path is overridable, and
        # the static spec's pass_path stays None (no built-in default) under an override.
        spec = credential_cls(pass_path_override=_ROUTED_PASS).spec
        assert spec.env_var == env_var
        assert spec.pass_path is None, "the static spec has no built-in default and an override never mutates it"
        assert env_var not in spec.conflicting_vars

    def test_missing_message_names_the_overridden_pass_path(self) -> None:
        # The loud CredentialError points at the entry the user must `pass insert` —
        # the OVERRIDDEN path, not the built-in default, so the fix instruction is right.
        credential = AnthropicApiKeyCredential(
            sources=[_FakeEnvSource({}), _FakePassSource({})], pass_path_override=_ROUTED_PASS
        )
        with pytest.raises(CredentialError) as excinfo:
            credential.resolve()
        assert _ROUTED_PASS in str(excinfo.value)


# (credential class, env var, routing setting, conflicting env var | None) — every
# Anthropic/Orca credential is default-less: it resolves from env or a configured
# per-account `pass` entry, and fails loud (naming the setting) when neither exists.
_NO_DEFAULT_CREDENTIALS = [
    pytest.param(AnthropicSubscriptionCredential, _OAUTH_ENV, _OAUTH_SETTING, _API_KEY_ENV, id="subscription"),
    pytest.param(AnthropicApiKeyCredential, _API_KEY_ENV, _API_KEY_SETTING, _OAUTH_ENV, id="metered"),
    pytest.param(OrcaRouterCredential, _ORCA_ENV, _ORCA_SETTING, None, id="orca"),
]
_no_default_case = pytest.mark.parametrize(
    ("credential_cls", "env_var", "setting", "conflicting"), _NO_DEFAULT_CREDENTIALS
)


class TestNoDefaultPassPath:
    """No credential has a built-in default ``pass`` path: env or configured routing, else loud."""

    @_no_default_case
    def test_no_override_no_env_fails_loud_naming_the_routing_setting(
        self, credential_cls: type[Credential], env_var: str, setting: str, conflicting: str | None
    ) -> None:
        credential = credential_cls(sources=[_FakeEnvSource({}), _FakePassSource({})])
        with pytest.raises(CredentialError) as excinfo:
            credential.resolve()
        message = str(excinfo.value)
        assert env_var in message, "names the env var the user can set"
        assert setting in message, "names the routing setting to configure instead of a dead default"
        if conflicting is not None:
            assert conflicting in message, "names the conflicting credential it never falls back to"

    @_no_default_case
    def test_no_override_resolves_from_env_when_present(
        self, credential_cls: type[Credential], env_var: str, setting: str, conflicting: str | None
    ) -> None:
        assert credential_cls(sources=[_FakeEnvSource({env_var: "from-env"})]).resolve() == "from-env"

    @_no_default_case
    def test_pass_source_skips_the_none_pass_path_without_reading_pass(
        self, credential_cls: type[Credential], env_var: str, setting: str, conflicting: str | None
    ) -> None:
        with patch("teatree.llm.credentials.read_pass") as read_pass:
            assert PassSource().lookup(credential_cls().spec) is None
        read_pass.assert_not_called()


class TestOrcaRouterCredential:
    """The ``pydantic_ai`` harness's BYOK provider — orthogonal to the Anthropic credentials."""

    def test_is_a_credential(self) -> None:
        assert issubclass(OrcaRouterCredential, Credential)

    def test_spec_has_no_default_pass_path(self) -> None:
        spec = OrcaRouterCredential().spec
        assert spec.env_var == _ORCA_ENV
        assert spec.pass_path is None
        assert spec.routing_setting == _ORCA_SETTING

    def test_declares_no_conflicting_vars(self) -> None:
        # OrcaRouter is an orthogonal provider — applying it strips nothing.
        assert OrcaRouterCredential().spec.conflicting_vars == ()

    def test_resolves_from_env(self) -> None:
        credential = OrcaRouterCredential(sources=[_FakeEnvSource({_ORCA_ENV: "orca-key-env"})])
        assert credential.resolve() == "orca-key-env"

    def test_falls_through_to_the_configured_pass_path(self) -> None:
        # No built-in default: pass resolution needs the configured `orca_router_pass_path`
        # (injected as an override).
        credential = OrcaRouterCredential(
            sources=[_FakeEnvSource({_ORCA_ENV: None}), _FakePassSource({_ORCA_PASS: "orca-key-pass"})],
            pass_path_override=_ORCA_PASS,
        )
        assert credential.resolve() == "orca-key-pass"

    def test_raises_loud_when_absent_naming_the_setting(self) -> None:
        credential = OrcaRouterCredential(sources=[_FakeEnvSource({}), _FakePassSource({})])
        with pytest.raises(CredentialError) as excinfo:
            credential.resolve()
        assert _ORCA_ENV in str(excinfo.value)
        assert _ORCA_SETTING in str(excinfo.value)

    def test_child_env_does_not_strip_anthropic_credentials(self) -> None:
        credential = OrcaRouterCredential(sources=[_FakeEnvSource({_ORCA_ENV: "orca-key"})])
        base = {_OAUTH_ENV: "sub-token", _API_KEY_ENV: "sk-env"}
        child = credential.child_env(base)
        assert child[_ORCA_ENV] == "orca-key"
        assert child[_OAUTH_ENV] == "sub-token"
        assert child[_API_KEY_ENV] == "sk-env"


class TestResolveOrcaRouterProviderConfig:
    """The full OpenAI-compatible provider config: BYOK key + endpoint."""

    def test_resolves_both_halves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_ORCA_BASE_URL_ENV, "https://orca.example/v1")
        credential = OrcaRouterCredential(sources=[_FakeEnvSource({_ORCA_ENV: "orca-key"})])
        config = resolve_orca_router_provider_config(credential=credential)
        assert config.api_key == "orca-key"
        assert config.base_url == "https://orca.example/v1"

    def test_missing_base_url_raises_loud(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(_ORCA_BASE_URL_ENV, raising=False)
        credential = OrcaRouterCredential(sources=[_FakeEnvSource({_ORCA_ENV: "orca-key"})])
        with pytest.raises(CredentialError) as excinfo:
            resolve_orca_router_provider_config(credential=credential)
        assert _ORCA_BASE_URL_ENV in str(excinfo.value)

    def test_missing_api_key_raises_loud_even_with_base_url_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No default `pass` path: an unconfigured key fails loud naming orca_router_pass_path.
        monkeypatch.setenv(_ORCA_BASE_URL_ENV, "https://orca.example/v1")
        credential = OrcaRouterCredential(sources=[_FakeEnvSource({}), _FakePassSource({})])
        with pytest.raises(CredentialError) as excinfo:
            resolve_orca_router_provider_config(credential=credential)
        assert _ORCA_SETTING in str(excinfo.value)

    def test_default_credential_is_a_fresh_orca_router_credential(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No injected credential: resolves via the real env-then-pass chain.
        monkeypatch.setenv(_ORCA_BASE_URL_ENV, "https://orca.example/v1")
        monkeypatch.setenv(_ORCA_ENV, "orca-key-from-real-env")
        config = resolve_orca_router_provider_config()
        assert config.api_key == "orca-key-from-real-env"


class TestForbiddenVarsRefuseRatherThanStrip:
    """``forbidden_vars`` names a misconfiguration to surface, not a fallback to remove.

    The subscription credential forbids ``ANTHROPIC_BASE_URL``: plan auth is valid
    only against Anthropic's own endpoint, and the ``claude`` CLI reads that variable
    from an inherited env. Stripping it silently would leave an operator believing a
    gateway was in use, so ``child_env`` raises instead.
    """

    def test_subscription_child_env_refuses_a_base_url_redirect(self) -> None:
        credential = AnthropicSubscriptionCredential(sources=[_FakeEnvSource({_OAUTH_ENV: "oat-1"})])
        with pytest.raises(CredentialError) as excinfo:
            credential.child_env({"ANTHROPIC_BASE_URL": "https://gateway.example.invalid/v1"})
        assert "ANTHROPIC_BASE_URL" in str(excinfo.value)
        assert "api_key" in str(excinfo.value), "the refusal must name the sanctioned remedy"

    def test_the_refusal_precedes_resolution_so_it_names_the_real_problem(self) -> None:
        # No credential is resolvable either; the base-URL refusal must still win, or
        # the operator is sent chasing a missing token instead of the redirect.
        credential = AnthropicSubscriptionCredential(sources=[_FakeEnvSource({}), _FakePassSource({})])
        with pytest.raises(CredentialError) as excinfo:
            credential.child_env({"ANTHROPIC_BASE_URL": "https://gateway.example.invalid/v1"})
        assert "ANTHROPIC_BASE_URL" in str(excinfo.value)

    def test_an_empty_redirect_value_expresses_nothing_and_is_allowed(self) -> None:
        credential = AnthropicSubscriptionCredential(sources=[_FakeEnvSource({_OAUTH_ENV: "oat-1"})])
        env = credential.child_env({"ANTHROPIC_BASE_URL": "   "})
        assert env[_OAUTH_ENV] == "oat-1"

    def test_the_metered_key_still_carries_a_redirect_through(self) -> None:
        # The sanctioned gateway / Bedrock / third-party-provider shape: legal here,
        # which is exactly why this is not a conflict of the credential pair.
        credential = AnthropicApiKeyCredential(sources=[_FakeEnvSource({_API_KEY_ENV: "sk-ant-key"})])
        env = credential.child_env({"ANTHROPIC_BASE_URL": "https://gateway.example.invalid/v1"})
        assert env["ANTHROPIC_BASE_URL"] == "https://gateway.example.invalid/v1"
        assert env[_API_KEY_ENV] == "sk-ant-key"
