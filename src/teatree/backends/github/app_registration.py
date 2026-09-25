"""Exchange a GitHub App manifest-flow code for credentials, persisted to ``pass`` (#4795).

``POST /app-manifests/{code}/conversions`` is GitHub's one-shot callback after
an operator completes App creation in the browser: it returns the App's
identity AND its freshly-minted secrets (private key, webhook secret, client
secret) in one payload GitHub never re-sends. :func:`exchange_manifest_code`
writes every secret straight to the ``pass`` store
(:func:`teatree.utils.secrets.write_pass_with_backup`) and returns only
non-secret metadata — the secret values never appear in a log, a CLI echo, or
this function's return value (the canary
``tests/test_github_app_secret_canary.py`` proves this end to end).
"""

from dataclasses import dataclass

import httpx

from teatree.utils.secrets import write_pass_with_backup

CONVERSION_URL = "https://api.github.com/app-manifests/{code}/conversions"

PRIVATE_KEY_PASS_KEY = "github-app/private-key"  # noqa: S105 — a `pass` key NAME, not a credential
WEBHOOK_SECRET_PASS_KEY = "github-app/webhook-secret"  # noqa: S105 — a `pass` key NAME, not a credential
CLIENT_SECRET_PASS_KEY = "github-app/client-secret"  # noqa: S105 — a `pass` key NAME, not a credential


@dataclass(frozen=True, slots=True)
class RegisteredApp:
    """Non-secret App identity returned after registration — no key/secret fields, ever."""

    app_id: str
    slug: str
    name: str
    html_url: str
    client_id: str


def exchange_manifest_code(code: str, *, client: httpx.Client | None = None) -> RegisteredApp:
    """Complete App registration: exchange *code*, persist secrets, return identity only.

    Raises :class:`httpx.HTTPStatusError` on a failed exchange — no secret is
    persisted unless the conversion actually succeeded (an all-or-nothing
    write: the three ``write_pass_with_backup`` calls only run after
    ``raise_for_status``).
    """
    transport_client = client or httpx.Client()
    response = transport_client.post(
        CONVERSION_URL.format(code=code), headers={"Accept": "application/vnd.github+json"}
    )
    response.raise_for_status()
    body = response.json()

    write_pass_with_backup(PRIVATE_KEY_PASS_KEY, body["pem"], echo=lambda _msg: None)
    write_pass_with_backup(WEBHOOK_SECRET_PASS_KEY, body["webhook_secret"], echo=lambda _msg: None)
    write_pass_with_backup(CLIENT_SECRET_PASS_KEY, body["client_secret"], echo=lambda _msg: None)

    return RegisteredApp(
        app_id=str(body["id"]),
        slug=body.get("slug", ""),
        name=body.get("name", ""),
        html_url=body.get("html_url", ""),
        client_id=body.get("client_id", ""),
    )


__all__ = [
    "CLIENT_SECRET_PASS_KEY",
    "PRIVATE_KEY_PASS_KEY",
    "WEBHOOK_SECRET_PASS_KEY",
    "RegisteredApp",
    "exchange_manifest_code",
]
