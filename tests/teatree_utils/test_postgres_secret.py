"""Tests for ``teatree.utils.postgres_secret`` — pass-key resolution helpers."""

import os
import warnings
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.utils import postgres_secret
from teatree.utils.postgres_secret import (
    PASS_KEY_ENV,
    POSTGRES_PASSWORD_ENV,
    RESOLVER_ENV,
    PostgresPasswordUnavailableError,
    ensure_postgres_pass_entry,
    extract_literal_from_cache,
    postgres_pass_key,
    remove_postgres_pass_entry,
    resolve_postgres_password,
)


@pytest.fixture(autouse=True)
def _reset_deprecation_state() -> None:
    """Reset the one-shot deprecation warning flag between tests."""
    postgres_secret._reset_literal_deprecation_state()


class TestPostgresPassKey:
    def test_returns_namespaced_key_for_ticket(self) -> None:
        assert postgres_pass_key("123") == "teatree/wt/123/postgres"

    def test_stringifies_non_string_ids(self) -> None:
        assert postgres_pass_key(456) == "teatree/wt/456/postgres"

    def test_rejects_empty_ticket_id(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            postgres_pass_key("")

    def test_rejects_whitespace_ticket_id(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            postgres_pass_key("   ")


class TestResolvePostgresPassword:
    def test_prefers_pass_key_when_pass_resolves(self) -> None:
        env = {PASS_KEY_ENV: "teatree/wt/42/postgres", POSTGRES_PASSWORD_ENV: "literal"}
        with patch.object(postgres_secret.secrets, "read_pass", return_value="from-pass") as mock:
            assert resolve_postgres_password(env) == "from-pass"
        mock.assert_called_once_with("teatree/wt/42/postgres")

    def test_falls_back_to_resolver_when_pass_empty(self, tmp_path: Path) -> None:
        # Write a tiny shell resolver that echoes a fixed secret.
        resolver = tmp_path / "resolver"
        resolver.write_text("#!/bin/sh\necho from-resolver\n")
        resolver.chmod(0o755)
        env = {
            PASS_KEY_ENV: "teatree/wt/42/postgres",
            RESOLVER_ENV: str(resolver),
        }
        with patch.object(postgres_secret.secrets, "read_pass", return_value=""):
            assert resolve_postgres_password(env) == "from-resolver"

    def test_falls_back_to_literal_with_deprecation_warning(self) -> None:
        env = {POSTGRES_PASSWORD_ENV: "legacy-literal"}
        with (
            patch.object(postgres_secret.secrets, "read_pass", return_value=""),
            warnings.catch_warnings(record=True) as caught,
        ):
            warnings.simplefilter("always")
            assert resolve_postgres_password(env) == "legacy-literal"
        kinds = [w.category for w in caught]
        assert DeprecationWarning in kinds

    def test_returns_empty_when_no_source_available(self) -> None:
        with patch.object(postgres_secret.secrets, "read_pass", return_value=""):
            assert resolve_postgres_password({}) == ""

    def test_deprecation_warning_fires_only_once_per_process(self) -> None:
        env = {POSTGRES_PASSWORD_ENV: "literal"}
        with (
            patch.object(postgres_secret.secrets, "read_pass", return_value=""),
            warnings.catch_warnings(record=True) as first_batch,
        ):
            warnings.simplefilter("always")
            resolve_postgres_password(env)
            resolve_postgres_password(env)
        deprecations = [w for w in first_batch if w.category is DeprecationWarning]
        assert len(deprecations) == 1

    def test_defaults_to_os_environ_when_env_not_provided(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(PASS_KEY_ENV, "teatree/wt/99/postgres")
        monkeypatch.delenv(RESOLVER_ENV, raising=False)
        monkeypatch.delenv(POSTGRES_PASSWORD_ENV, raising=False)
        with patch.object(postgres_secret.secrets, "read_pass", return_value="env-resolved"):
            assert resolve_postgres_password() == "env-resolved"


class TestEnsurePostgresPassEntry:
    def test_writes_to_pass_under_canonical_key(self) -> None:
        with patch.object(postgres_secret.secrets, "write_pass", return_value=True) as mock:
            key = ensure_postgres_pass_entry("777", "sup3r-s3cret")
        assert key == "teatree/wt/777/postgres"
        mock.assert_called_once_with("teatree/wt/777/postgres", "sup3r-s3cret")

    def test_raises_when_pass_write_fails(self) -> None:
        with (
            patch.object(postgres_secret.secrets, "write_pass", return_value=False),
            pytest.raises(PostgresPasswordUnavailableError, match="pass is not installed"),
        ):
            ensure_postgres_pass_entry("888", "secret")

    def test_rejects_empty_password(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            ensure_postgres_pass_entry("888", "")


class TestRemovePostgresPassEntry:
    def test_calls_remove_pass_with_canonical_key(self) -> None:
        with patch.object(postgres_secret.secrets, "remove_pass", return_value=True) as mock:
            assert remove_postgres_pass_entry("321") is True
        mock.assert_called_once_with("teatree/wt/321/postgres")

    def test_returns_false_when_pass_remove_fails(self) -> None:
        with patch.object(postgres_secret.secrets, "remove_pass", return_value=False):
            assert remove_postgres_pass_entry("321") is False


class TestExtractLiteralFromCache:
    def test_returns_value_for_existing_literal(self, tmp_path: Path) -> None:
        cache = tmp_path / ".t3-env.cache"
        cache.write_text("FOO=bar\nPOSTGRES_PASSWORD=abc123\nBAZ=qux\n", encoding="utf-8")
        assert extract_literal_from_cache(cache) == "abc123"

    def test_returns_empty_when_cache_missing(self, tmp_path: Path) -> None:
        assert extract_literal_from_cache(tmp_path / "missing.cache") == ""

    def test_returns_empty_when_only_pass_key_present(self, tmp_path: Path) -> None:
        cache = tmp_path / ".t3-env.cache"
        cache.write_text("POSTGRES_PASSWORD_PASS_KEY=teatree/wt/1/postgres\n", encoding="utf-8")
        assert extract_literal_from_cache(cache) == ""

    def test_does_not_leak_password_to_logger(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        cache = tmp_path / ".t3-env.cache"
        cache.write_text("POSTGRES_PASSWORD=swordfish-literal\n", encoding="utf-8")
        caplog.set_level("DEBUG", logger="teatree.utils.postgres_secret")
        extract_literal_from_cache(cache)
        assert "swordfish-literal" not in caplog.text


class TestResolverCommandInvocation:
    """End-to-end exercise of the T3_SECRET_RESOLVER fallback.

    Uses a real executable script under tmp_path rather than mocking
    ``run_checked`` — this verifies the resolver wiring stays honest when
    the resolver echoes the secret and exits zero.
    """

    def test_resolver_receives_pass_key_as_argument(self, tmp_path: Path) -> None:
        resolver = tmp_path / "resolver"
        resolver.write_text('#!/bin/sh\nprintf "key:%s\\n" "$1"\n')
        resolver.chmod(0o755)
        env = {PASS_KEY_ENV: "teatree/wt/1/postgres", RESOLVER_ENV: str(resolver)}
        with patch.object(postgres_secret.secrets, "read_pass", return_value=""):
            value = resolve_postgres_password(env)
        assert value == "key:teatree/wt/1/postgres"

    def test_resolver_failure_falls_through_to_literal(self, tmp_path: Path) -> None:
        resolver = tmp_path / "resolver"
        resolver.write_text("#!/bin/sh\nexit 1\n")
        resolver.chmod(0o755)
        env = {RESOLVER_ENV: str(resolver), POSTGRES_PASSWORD_ENV: "literal"}
        with (
            patch.object(postgres_secret.secrets, "read_pass", return_value=""),
            warnings.catch_warnings(),
        ):
            warnings.simplefilter("ignore", DeprecationWarning)
            assert resolve_postgres_password(env) == "literal"

    def test_missing_resolver_falls_through(self) -> None:
        env = {RESOLVER_ENV: "/no/such/binary", POSTGRES_PASSWORD_ENV: "literal"}
        with (
            patch.object(postgres_secret.secrets, "read_pass", return_value=""),
            warnings.catch_warnings(),
        ):
            warnings.simplefilter("ignore", DeprecationWarning)
            assert resolve_postgres_password(env) == "literal"


def test_module_does_not_log_literal_value(caplog: pytest.LogCaptureFixture) -> None:
    """Verify the literal value never reaches the log stream."""
    env = {PASS_KEY_ENV: "teatree/wt/9/postgres", POSTGRES_PASSWORD_ENV: "secret-literal"}
    caplog.set_level("DEBUG", logger="teatree.utils.postgres_secret")
    with patch.object(postgres_secret.secrets, "read_pass", return_value=""):
        resolve_postgres_password(env)
    assert "secret-literal" not in caplog.text


def test_env_resolution_uses_os_environ_by_default_without_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolution must not mutate ``os.environ`` so cache misses do not leak."""
    monkeypatch.setenv(PASS_KEY_ENV, "teatree/wt/9/postgres")
    snapshot = dict(os.environ)
    with patch.object(postgres_secret.secrets, "read_pass", return_value="x"):
        resolve_postgres_password()
    assert dict(os.environ) == snapshot
