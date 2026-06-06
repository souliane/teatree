"""Cross-layer connector keys owned by the domain.

``GRANTED_SCOPES_KEY`` is the dict key under which a Slack ``auth.test``
response carries its parsed granted OAuth scopes. Both the backend transport
(``backends.slack_scopes``, which writes it) and the domain preflight guard
(``core.connector_preflight``, which reads it) need the same key. It lives in
``core`` so the guard never imports ``backends`` (#1922); ``slack_scopes``
re-imports it as the allowed ``backends → core`` direction.
"""

GRANTED_SCOPES_KEY = "_granted_scopes"
