# test-path: cross-cutting
"""Secret canary for the GitHub App integration (#4795).

Plants FAKE, obviously-distinguishable secret values through the exact seams the
App flow uses (the manifest-conversion response, an installation-token mint) and
asserts they never surface in anything an operator or a log line would show —
the manifest JSON, a CLI subcommand's printed output, or a Python ``repr()`` of
every return value the flow produces. A canary that only ever passes guards
nothing (see ``tests/test_audit_canary.py``); the point is that EVERY assertion
below would catch a real leak if one of these functions ever grew a stray
``str(body)`` or ``logger.info(f"...{secret}...")``.
"""

import datetime as dt
from io import StringIO
from unittest import mock

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.core.management import call_command
from django.test import TestCase

from teatree.backends.github import app_registration
from teatree.backends.github.app_auth import InstallationTokenCache, sign_app_jwt
from teatree.backends.github.app_manifest import build_manifest
from teatree.backends.github.app_registration import exchange_manifest_code
from teatree.core.models import GitHubAppInstallation

# Deliberately implausible strings — if one of these appears anywhere it can only
# be because a real code path echoed the secret, never a coincidental match.
_CANARY_PRIVATE_KEY = (
    "-----BEGIN RSA PRIVATE KEY-----\nCANARY_PEM_MUST_NEVER_LEAK_7f3a9c\n-----END RSA PRIVATE KEY-----"
)
_CANARY_WEBHOOK_SECRET = "CANARY_WEBHOOK_SECRET_MUST_NEVER_LEAK_b21e04"
_CANARY_CLIENT_SECRET = "CANARY_CLIENT_SECRET_MUST_NEVER_LEAK_91cd6a"
_CANARY_INSTALLATION_TOKEN = "ghs_CANARY_INSTALLATION_TOKEN_MUST_NEVER_LEAK_44de"

_ALL_CANARIES = (
    _CANARY_PRIVATE_KEY,
    _CANARY_WEBHOOK_SECRET,
    _CANARY_CLIENT_SECRET,
    _CANARY_INSTALLATION_TOKEN,
)


def _assert_no_canary_leaked(*artifacts: object) -> None:
    for artifact in artifacts:
        rendered = repr(artifact)
        for canary in _ALL_CANARIES:
            assert canary not in rendered, f"secret canary {canary!r} leaked into {rendered!r}"


class TestManifestNeverCarriesASecret(TestCase):
    def test_build_manifest_output(self) -> None:
        manifest = build_manifest(name="x", url="https://e.com", webhook_url="https://e.com/h/")
        _assert_no_canary_leaked(manifest)


class TestRegistrationExchangeNeverReturnsOrLogsASecret(TestCase):
    def test_registered_app_return_value(self) -> None:
        body = {
            "id": 1,
            "slug": "x",
            "name": "x",
            "html_url": "https://github.com/apps/x",
            "client_id": "Iv1.public",
            "client_secret": _CANARY_CLIENT_SECRET,
            "pem": _CANARY_PRIVATE_KEY,
            "webhook_secret": _CANARY_WEBHOOK_SECRET,
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(201, json=body)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        writes: list[tuple[str, str]] = []
        with mock.patch.object(
            app_registration, "write_pass_with_backup", lambda key, value, echo: writes.append((key, value)) or ""
        ):
            registered = exchange_manifest_code("test-code", client=client)

        _assert_no_canary_leaked(registered)
        # The secrets DID get persisted (to the store, not echoed) — the three
        # ``pass`` writes are the only place a canary is allowed to appear.
        assert dict(writes)[app_registration.PRIVATE_KEY_PASS_KEY] == _CANARY_PRIVATE_KEY
        assert dict(writes)[app_registration.WEBHOOK_SECRET_PASS_KEY] == _CANARY_WEBHOOK_SECRET
        assert dict(writes)[app_registration.CLIENT_SECRET_PASS_KEY] == _CANARY_CLIENT_SECRET

    def test_cli_register_subcommand_stdout(self) -> None:
        body = {
            "id": 1,
            "slug": "x",
            "name": "x",
            "html_url": "https://github.com/apps/x",
            "client_id": "Iv1.public",
            "client_secret": _CANARY_CLIENT_SECRET,
            "pem": _CANARY_PRIVATE_KEY,
            "webhook_secret": _CANARY_WEBHOOK_SECRET,
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(201, json=body)

        with (
            mock.patch("httpx.Client", return_value=httpx.Client(transport=httpx.MockTransport(handler))),
            mock.patch.object(app_registration, "write_pass_with_backup", return_value=""),
        ):
            out = StringIO()
            call_command("github_app", "register", "test-code", stdout=out)

        for canary in _ALL_CANARIES:
            assert canary not in out.getvalue()


class TestInstallationTokenMintingNeverLeaksThePrivateKeyOrToken(TestCase):
    def test_signed_jwt_carries_no_raw_key_material(self) -> None:
        now = dt.datetime(2026, 9, 25, tzinfo=dt.UTC)
        token = sign_app_jwt(app_id="1", private_key_pem=_generate_test_pem(), now=now)
        assert _CANARY_PRIVATE_KEY not in token

    def test_status_command_never_prints_a_minted_token(self) -> None:
        GitHubAppInstallation.objects.create(installation_id=1, app_id=1, account_login="x")
        out = StringIO()
        call_command("github_app", "status", stdout=out)
        assert _CANARY_INSTALLATION_TOKEN not in out.getvalue()

    def test_token_cache_repr_excludes_the_private_key_and_cached_tokens(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(201, json={"token": _CANARY_INSTALLATION_TOKEN, "expires_at": "2026-09-25T13:00:00Z"})

        pem = _generate_test_pem()
        cache = InstallationTokenCache(
            app_id="1", private_key_pem=pem, client=httpx.Client(transport=httpx.MockTransport(handler))
        )
        token = cache.token_for(1, now=dt.datetime(2026, 9, 25, 12, tzinfo=dt.UTC))
        # The token IS the return value by design (a caller needs it to
        # authenticate); what must NEVER happen is the dataclass's own repr —
        # what an accidental ``logger.debug(cache)`` would print — embedding
        # either the private key or a cached installation token.
        assert token == _CANARY_INSTALLATION_TOKEN
        assert pem not in repr(cache)
        assert _CANARY_INSTALLATION_TOKEN not in repr(cache)


def _generate_test_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
