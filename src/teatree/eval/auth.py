"""OAuth-token resolution for the metered ``sdk`` eval lane.

``claude -p`` authenticates from ``CLAUDE_CODE_OAUTH_TOKEN`` — the OAuth token
from ``claude setup-token``. It reaches the CLI two ways, and this module is the
single resolver both rely on.

CI wires it from the ``CLAUDE_CODE_OAUTH_TOKEN`` repo secret as a plain env var;
an already-set env var always wins — :func:`ensure_oauth_token` never overwrites
it. Locally it lives in the ``pass`` store under ``anthropic/oauth-token``: when
the env var is absent (or empty), the resolver falls back to ``pass`` and
*exports* the value into ``os.environ`` so the host runner's
:func:`~teatree.eval.isolation.isolated_claude_env` copy and the docker
``-e CLAUDE_CODE_OAUTH_TOKEN`` pass-through both carry it without a manual
``export``.

A genuinely missing token resolves to ``None`` and exports nothing — masking the
absence with an empty env var would defeat the ``--require-executed`` fail-loud
gate (an empty token still fails auth, but silently looks "set").
"""

import os

from teatree.utils.secrets import read_pass

OAUTH_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"  # noqa: S105 — env-var name, not a secret
OAUTH_TOKEN_PASS_KEY = "anthropic/oauth-token"  # noqa: S105 — pass key, not a secret


def ensure_oauth_token() -> str | None:
    """Resolve the OAuth token (env wins, else ``pass``) and export it; return it or ``None``."""
    existing = os.environ.get(OAUTH_TOKEN_ENV)
    if existing:
        return existing
    from_pass = read_pass(OAUTH_TOKEN_PASS_KEY)
    if not from_pass:
        return None
    os.environ[OAUTH_TOKEN_ENV] = from_pass
    return from_pass
