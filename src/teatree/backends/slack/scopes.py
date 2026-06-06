"""Slack OAuth-scope surfacing for ``auth.test``.

Slack reports the granted OAuth scopes of a token in the ``X-OAuth-Scopes``
HTTP *response header*, not in the JSON body. ``SlackBotBackend.auth_test``
surfaces the parsed set under :data:`GRANTED_SCOPES_KEY` so a scope-preflight
guard can read it without re-issuing the request. Keeping the parsing here
(separate from the backend transport) lets both the backend and the
connector-preflight guard share one source of truth.
"""

from teatree.core.connector_keys import GRANTED_SCOPES_KEY
from teatree.types import RawAPIDict

OAUTH_SCOPES_HEADER = "X-OAuth-Scopes"


def parse_oauth_scopes(header: str) -> frozenset[str]:
    """Parse a comma-separated ``X-OAuth-Scopes`` header into a clean scope set."""
    return frozenset(scope.strip() for scope in header.split(",") if scope.strip())


def attach_granted_scopes(body: RawAPIDict, header_value: str) -> RawAPIDict:
    """Attach the scopes parsed from *header_value* onto *body* under :data:`GRANTED_SCOPES_KEY`.

    *body* (the ``auth.test`` JSON response) is returned mutated in place; its
    Slack-native keys (``ok`` / ``user_id`` / ``bot_id``) are left untouched.
    """
    body[GRANTED_SCOPES_KEY] = sorted(parse_oauth_scopes(header_value))
    return body


__all__ = [
    "GRANTED_SCOPES_KEY",
    "OAUTH_SCOPES_HEADER",
    "attach_granted_scopes",
    "parse_oauth_scopes",
]
