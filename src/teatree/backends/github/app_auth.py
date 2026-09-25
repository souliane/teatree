"""RS256 GitHub App JWT signing + installation-token minting/caching (#4795).

Hand-rolled RS256 via ``cryptography`` (already a locked transitive dependency)
rather than adding ``PyJWT`` as a new direct one — see the architecture
pre-check. GitHub App auth needs exactly one JWS operation (sign, RS256, two
claims); a full JWT library is unneeded surface for that.
"""

import base64
import datetime as dt
import json
from dataclasses import dataclass, field

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

#: GitHub rejects a JWT whose ``iat`` is in the future under minor clock drift
#: between this host and GitHub's — backdating absorbs that (GitHub's own
#: recommendation). ``exp`` is capped at 10 minutes, GitHub's own maximum.
_CLOCK_DRIFT_TOLERANCE = dt.timedelta(seconds=60)
_JWT_TTL = dt.timedelta(minutes=10)

#: An installation token's real TTL is ~1h; refreshed this long before its
#: reported expiry so a request in flight never races a mid-call expiry.
_REFRESH_MARGIN = dt.timedelta(minutes=2)

_INSTALLATION_TOKEN_URL = "https://api.github.com/app/installations/{installation_id}/access_tokens"  # noqa: S105 — API URL, not a secret


def sign_app_jwt(*, app_id: str, private_key_pem: str, now: dt.datetime | None = None) -> str:
    """A signed RS256 App JWT — ``Authorization: Bearer <token>`` for App-level endpoints."""
    moment = now or dt.datetime.now(dt.UTC)
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {
        "iat": int((moment - _CLOCK_DRIFT_TOLERANCE).timestamp()),
        "exp": int((moment + _JWT_TTL).timestamp()),
        "iss": app_id,
    }
    header_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{header_b64}.{payload_b64}".encode()
    private_key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    if not isinstance(private_key, RSAPrivateKey):
        msg = "GitHub App private key must be an RSA key"
        raise TypeError(msg)
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header_b64}.{payload_b64}.{_b64url(signature)}"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@dataclass(slots=True)
class _CachedToken:
    value: str
    expires_at: dt.datetime

    def usable_at(self, moment: dt.datetime) -> bool:
        return moment < self.expires_at - _REFRESH_MARGIN


@dataclass(slots=True)
class InstallationTokenCache:
    """Mints and caches per-installation access tokens, refreshed ahead of expiry.

    *client* is injectable (a test passes an ``httpx.MockTransport``-backed
    client) so no live network call is needed to test minting/caching/refresh.
    """

    app_id: str
    #: Excluded from ``repr()`` — an accidental ``logger.debug(cache)`` (or any
    #: str/repr of this dataclass) must never print the App's private key.
    private_key_pem: str = field(repr=False)
    client: httpx.Client = field(default_factory=httpx.Client, repr=False)
    _tokens: dict[int, _CachedToken] = field(default_factory=dict, repr=False)

    def token_for(self, installation_id: int, *, now: dt.datetime | None = None) -> str:
        moment = now or dt.datetime.now(dt.UTC)
        cached = self._tokens.get(installation_id)
        if cached is not None and cached.usable_at(moment):
            return cached.value
        minted = self._mint(installation_id, now=moment)
        self._tokens[installation_id] = minted
        return minted.value

    def _mint(self, installation_id: int, *, now: dt.datetime) -> _CachedToken:
        jwt = sign_app_jwt(app_id=self.app_id, private_key_pem=self.private_key_pem, now=now)
        response = self.client.post(
            _INSTALLATION_TOKEN_URL.format(installation_id=installation_id),
            headers={"Authorization": f"Bearer {jwt}", "Accept": "application/vnd.github+json"},
        )
        response.raise_for_status()
        body = response.json()
        expires_at = dt.datetime.strptime(body["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)
        return _CachedToken(value=body["token"], expires_at=expires_at)


__all__ = ["InstallationTokenCache", "sign_app_jwt"]
