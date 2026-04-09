"""Teatree overlay settings — configure like Django settings.py.

Plain constants for static values. For secrets, specify the ``pass`` key
and OverlayConfig will read it at runtime.
"""

# ── Code host ───────────────────────────────────────────────────────

GITHUB_OWNER: str = "souliane"
GITHUB_PROJECT_NUMBER: int = 2
GITHUB_TOKEN_PASS_KEY: str = "github/token"  # noqa: S105 — pass key name, not a secret
GITLAB_TOKEN_PASS_KEY: str = "gitlab/pat"  # noqa: S105 — pass key name, not a secret

# ── Workflow ────────────────────────────────────────────────────────

REQUIRE_TICKET: bool = True
