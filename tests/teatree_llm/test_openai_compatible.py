"""One backend for every OpenAI-compatible API — generic settings, no per-provider class.

souliane/teatree#3666: the router the ``pydantic_ai`` harness rides is just an
OpenAI-compatible API, yet it had a dedicated credential class and four
provider-named settings. The provider is ordinary configuration now: a base URL,
a model name, and the NAME of a credential-store entry, all generic.

The credential-store entry NAME is the only credential surface here — a secret
VALUE never appears in config, only the entry it is read from.
"""

import pytest

from teatree.llm import credentials
from teatree.llm.credentials import CredentialError
from teatree.llm.openai_compatible import (
    OPENAI_COMPATIBLE_API_KEY_ENV,
    OPENAI_COMPATIBLE_BASE_URL_ENV,
    OpenAICompatibleBackend,
    OpenAICompatibleCredential,
    resolve_openai_compatible_backend,
    warn_retired_env_vars,
)


class _FakeSource:
    def __init__(self, value: str | None) -> None:
        self.value = value
        self.seen_pass_path: str | None = None

    def lookup(self, spec: object) -> str | None:
        self.seen_pass_path = getattr(spec, "pass_path", None)
        return self.value


class TestCredentialIsGeneric:
    """The credential names no provider — only the generic env var and routing setting."""

    def test_env_var_is_provider_neutral(self) -> None:
        assert OpenAICompatibleCredential.spec.env_var == OPENAI_COMPATIBLE_API_KEY_ENV
        assert OPENAI_COMPATIBLE_API_KEY_ENV == "OPENAI_COMPATIBLE_API_KEY"

    def test_routing_setting_names_the_credential_store_entry(self) -> None:
        assert OpenAICompatibleCredential.spec.routing_setting == "openai_compatible_credential_entry"

    def test_no_built_in_pass_path_and_no_conflicting_vars(self) -> None:
        assert OpenAICompatibleCredential.spec.pass_path is None
        assert OpenAICompatibleCredential.spec.conflicting_vars == ()

    def test_configured_entry_name_is_the_pass_path_read(self) -> None:
        source = _FakeSource("secret-from-the-store")
        credential = OpenAICompatibleCredential(sources=[source], pass_path_override="factory/api-key")
        assert credential.resolve() == "secret-from-the-store"
        assert source.seen_pass_path == "factory/api-key"

    def test_missing_credential_names_the_generic_setting(self) -> None:
        credential = OpenAICompatibleCredential(sources=[_FakeSource(None)])
        with pytest.raises(CredentialError, match="openai_compatible_credential_entry"):
            credential.resolve()

    def test_child_env_leaves_the_anthropic_credentials_alone(self) -> None:
        credential = OpenAICompatibleCredential(sources=[_FakeSource("key")])
        child = credential.child_env({"CLAUDE_CODE_OAUTH_TOKEN": "sub", "ANTHROPIC_API_KEY": "sk"})
        assert child[OPENAI_COMPATIBLE_API_KEY_ENV] == "key"
        assert child["CLAUDE_CODE_OAUTH_TOKEN"] == "sub"
        assert child["ANTHROPIC_API_KEY"] == "sk"


class TestBackendResolution:
    """The backend is base URL + model + credential-store entry, and fails loud on a gap."""

    def _credential(self, value: str | None = "key") -> OpenAICompatibleCredential:
        return OpenAICompatibleCredential(sources=[_FakeSource(value)])

    def test_resolves_from_the_generic_settings(self) -> None:
        backend = resolve_openai_compatible_backend(
            base_url="https://example.invalid/v1",
            model="vendor/some-model",
            credential=self._credential(),
        )
        assert backend == OpenAICompatibleBackend(
            base_url="https://example.invalid/v1", model="vendor/some-model", api_key="key"
        )

    def test_absent_base_url_fails_loud_naming_the_setting(self) -> None:
        with pytest.raises(CredentialError, match="openai_compatible_base_url"):
            resolve_openai_compatible_backend(base_url="", model="m", credential=self._credential())

    def test_env_base_url_wins_over_an_empty_setting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(OPENAI_COMPATIBLE_BASE_URL_ENV, "https://env.invalid/v1")
        backend = resolve_openai_compatible_backend(base_url="", model="m", credential=self._credential())
        assert backend.base_url == "https://env.invalid/v1"

    def test_never_fabricates_a_default_endpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(OPENAI_COMPATIBLE_BASE_URL_ENV, raising=False)
        with pytest.raises(CredentialError):
            resolve_openai_compatible_backend(base_url="   ", model="m", credential=self._credential())


class TestNoProviderSpecificBackendRemains:
    """The bespoke provider credential class is gone, not aliased."""

    def test_llm_credentials_exposes_no_provider_specific_class(self) -> None:
        provider_named = [name for name in dir(credentials) if "orca" in name.lower()]
        assert provider_named == []


class TestWarnRetiredEnvVars:
    """A retired provider-specific env var, still exported, must not read as in-effect (#3666)."""

    def test_a_set_retired_var_without_its_successor_warns(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("ORCA_ROUTER_BASE_URL", "https://legacy.example")
        monkeypatch.delenv(OPENAI_COMPATIBLE_BASE_URL_ENV, raising=False)
        warn_retired_env_vars()
        stderr = capsys.readouterr().err
        assert "ORCA_ROUTER_BASE_URL" in stderr
        assert OPENAI_COMPATIBLE_BASE_URL_ENV in stderr
        assert "NO effect" in stderr

    def test_no_warning_when_the_successor_is_also_set(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The operator has migrated: retired var present but so is its successor.
        monkeypatch.setenv("ORCA_ROUTER_BASE_URL", "https://legacy.example")
        monkeypatch.setenv(OPENAI_COMPATIBLE_BASE_URL_ENV, "https://new.example")
        warn_retired_env_vars()
        assert capsys.readouterr().err == ""

    def test_no_warning_when_no_retired_var_is_set(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("ORCA_ROUTER_BASE_URL", raising=False)
        monkeypatch.delenv("ORCA_ROUTER_API_KEY", raising=False)
        warn_retired_env_vars()
        assert capsys.readouterr().err == ""
