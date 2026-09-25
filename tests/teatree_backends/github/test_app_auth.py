"""RS256 App-JWT signing + installation-token minting/caching (#4795).

Hand-rolled RS256 (via ``cryptography``, already a locked dependency) rather
than a new ``PyJWT`` dependency — see the architecture pre-check.
"""

import base64
import datetime as dt
import json

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from teatree.backends.github.app_auth import InstallationTokenCache, sign_app_jwt

_FIXED_NOW = dt.datetime(2026, 9, 25, 12, 0, 0, tzinfo=dt.UTC)


@pytest.fixture(scope="module")
def rsa_keypair() -> tuple[str, rsa.RSAPublicKey]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return pem, private_key.public_key()


def _b64url_decode(segment: str) -> bytes:
    padded = segment + "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(padded)


class TestSignAppJwt:
    def test_header_and_payload_shape(self, rsa_keypair: tuple[str, rsa.RSAPublicKey]) -> None:
        pem, _public = rsa_keypair
        token = sign_app_jwt(app_id="12345", private_key_pem=pem, now=_FIXED_NOW)
        header_b64, payload_b64, _sig_b64 = token.split(".")
        header = json.loads(_b64url_decode(header_b64))
        payload = json.loads(_b64url_decode(payload_b64))
        assert header == {"alg": "RS256", "typ": "JWT"}
        assert payload["iss"] == "12345"
        # ``iat`` is backdated ~60s for clock drift tolerance (GitHub's own guidance).
        assert payload["iat"] == int(_FIXED_NOW.timestamp()) - 60
        assert payload["exp"] == int(_FIXED_NOW.timestamp()) + 600

    def test_signature_verifies_against_the_public_key(self, rsa_keypair: tuple[str, rsa.RSAPublicKey]) -> None:
        pem, public_key = rsa_keypair
        token = sign_app_jwt(app_id="12345", private_key_pem=pem, now=_FIXED_NOW)
        header_b64, payload_b64, sig_b64 = token.split(".")
        signed_input = f"{header_b64}.{payload_b64}".encode()
        signature = _b64url_decode(sig_b64)
        public_key.verify(signature, signed_input, padding.PKCS1v15(), hashes.SHA256())  # raises on mismatch

    def test_a_tampered_payload_fails_verification(self, rsa_keypair: tuple[str, rsa.RSAPublicKey]) -> None:
        pem, public_key = rsa_keypair
        token = sign_app_jwt(app_id="12345", private_key_pem=pem, now=_FIXED_NOW)
        header_b64, payload_b64, sig_b64 = token.split(".")
        tampered_payload = json.loads(_b64url_decode(payload_b64))
        tampered_payload["iss"] = "99999"
        tampered_b64 = base64.urlsafe_b64encode(json.dumps(tampered_payload).encode()).rstrip(b"=").decode()
        signature = _b64url_decode(sig_b64)
        with pytest.raises(Exception):  # noqa: B017,PT011 — any cryptography InvalidSignature-family error
            public_key.verify(signature, f"{header_b64}.{tampered_b64}".encode(), padding.PKCS1v15(), hashes.SHA256())


class TestInstallationTokenCache:
    def _cache(self, *, pem: str, responses: list[httpx.Response]) -> InstallationTokenCache:
        calls = iter(responses)

        def handler(request: httpx.Request) -> httpx.Response:
            return next(calls)

        transport = httpx.MockTransport(handler)
        return InstallationTokenCache(
            app_id="12345",
            private_key_pem=pem,
            client=httpx.Client(transport=transport),
        )

    def test_mints_and_returns_a_fresh_token(self, rsa_keypair: tuple[str, rsa.RSAPublicKey]) -> None:
        pem, _public = rsa_keypair
        response = httpx.Response(201, json={"token": "ghs_abc123", "expires_at": "2026-09-25T13:00:00Z"})
        cache = self._cache(pem=pem, responses=[response])

        token = cache.token_for(555, now=_FIXED_NOW)

        assert token == "ghs_abc123"

    def test_a_second_call_within_expiry_reuses_the_cached_token(
        self, rsa_keypair: tuple[str, rsa.RSAPublicKey]
    ) -> None:
        pem, _public = rsa_keypair
        response = httpx.Response(201, json={"token": "ghs_abc123", "expires_at": "2026-09-25T13:00:00Z"})
        # Only ONE response queued — a second HTTP call would raise StopIteration.
        cache = self._cache(pem=pem, responses=[response])

        first = cache.token_for(555, now=_FIXED_NOW)
        second = cache.token_for(555, now=_FIXED_NOW + dt.timedelta(minutes=1))

        assert first == second == "ghs_abc123"

    def test_an_expired_token_is_re_minted(self, rsa_keypair: tuple[str, rsa.RSAPublicKey]) -> None:
        pem, _public = rsa_keypair
        responses = [
            httpx.Response(201, json={"token": "ghs_first", "expires_at": "2026-09-25T12:05:00Z"}),
            httpx.Response(201, json={"token": "ghs_second", "expires_at": "2026-09-25T13:05:00Z"}),
        ]
        cache = self._cache(pem=pem, responses=responses)

        first = cache.token_for(555, now=_FIXED_NOW)
        second = cache.token_for(555, now=_FIXED_NOW + dt.timedelta(minutes=10))

        assert first == "ghs_first"
        assert second == "ghs_second"

    def test_distinct_installations_get_distinct_cached_tokens(self, rsa_keypair: tuple[str, rsa.RSAPublicKey]) -> None:
        pem, _public = rsa_keypair
        responses = [
            httpx.Response(201, json={"token": "ghs_555", "expires_at": "2026-09-25T13:00:00Z"}),
            httpx.Response(201, json={"token": "ghs_777", "expires_at": "2026-09-25T13:00:00Z"}),
        ]
        cache = self._cache(pem=pem, responses=responses)

        token_555 = cache.token_for(555, now=_FIXED_NOW)
        token_777 = cache.token_for(777, now=_FIXED_NOW)

        assert token_555 == "ghs_555"
        assert token_777 == "ghs_777"
