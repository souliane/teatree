"""Manifest-code exchange persists secrets to ``pass`` and never returns them (#4795)."""

import httpx
import pytest

from teatree.backends.github import app_registration
from teatree.backends.github.app_registration import RegisteredApp, exchange_manifest_code


def _client(body: dict) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/app-manifests/test-code-123/conversions"
        return httpx.Response(201, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


_CONVERSION_BODY = {
    "id": 999,
    "slug": "teatree-souliane",
    "name": "teatree-souliane",
    "html_url": "https://github.com/apps/teatree-souliane",
    "client_id": "Iv1.abc123",
    "client_secret": "supersecretclientsecret",
    "pem": "-----BEGIN RSA PRIVATE KEY-----\nfakekeydata\n-----END RSA PRIVATE KEY-----",
    "webhook_secret": "supersecretwebhooksecret",
}


class TestExchangeManifestCode:
    def test_returns_only_non_secret_metadata(self, monkeypatch: pytest.MonkeyPatch) -> None:
        writes: list[tuple[str, str]] = []
        monkeypatch.setattr(
            app_registration, "write_pass_with_backup", lambda key, value, echo: writes.append((key, value)) or ""
        )

        result = exchange_manifest_code("test-code-123", client=_client(_CONVERSION_BODY))

        assert result == RegisteredApp(
            app_id="999",
            slug="teatree-souliane",
            name="teatree-souliane",
            html_url="https://github.com/apps/teatree-souliane",
            client_id="Iv1.abc123",
        )
        assert "supersecretclientsecret" not in str(result)
        assert "supersecretwebhooksecret" not in str(result)
        assert "fakekeydata" not in str(result)

    def test_persists_all_three_secrets_via_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        writes: list[tuple[str, str]] = []
        monkeypatch.setattr(
            app_registration, "write_pass_with_backup", lambda key, value, echo: writes.append((key, value)) or ""
        )

        exchange_manifest_code("test-code-123", client=_client(_CONVERSION_BODY))

        written_keys = {key for key, _ in writes}
        assert written_keys == {
            app_registration.PRIVATE_KEY_PASS_KEY,
            app_registration.WEBHOOK_SECRET_PASS_KEY,
            app_registration.CLIENT_SECRET_PASS_KEY,
        }
        written = dict(writes)
        assert written[app_registration.PRIVATE_KEY_PASS_KEY] == _CONVERSION_BODY["pem"]
        assert written[app_registration.WEBHOOK_SECRET_PASS_KEY] == _CONVERSION_BODY["webhook_secret"]
        assert written[app_registration.CLIENT_SECRET_PASS_KEY] == _CONVERSION_BODY["client_secret"]

    def test_a_failed_conversion_writes_no_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        writes: list[tuple[str, str]] = []
        monkeypatch.setattr(
            app_registration, "write_pass_with_backup", lambda key, value, echo: writes.append((key, value)) or ""
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"message": "Not Found"})

        client = httpx.Client(transport=httpx.MockTransport(handler))

        with pytest.raises(httpx.HTTPStatusError):
            exchange_manifest_code("bad-code", client=client)

        assert writes == []
