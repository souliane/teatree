"""Teatree overlay settings — configure like Django settings.py.

Plain constants for static values. For secrets, specify the ``pass`` key
and OverlayConfig will read it at runtime.
"""

# ── Code host ───────────────────────────────────────────────────────

GITHUB_OWNER: str = "souliane"
GITHUB_PROJECT_NUMBER: int = 2
GITHUB_TOKEN_PASS_KEY: str = "github/token"  # noqa: S105 — pass key name, not a secret
GITLAB_TOKEN_PASS_KEY: str = "gitlab/pat"  # noqa: S105 — pass key name, not a secret

# One human, several forge handles. The first handle is the canonical
# display name; a reassignment between two handles in a group is a
# self-handoff and never surfaces on the statusline. The operator's own
# private handles belong in ``~/.teatree.toml`` under
# ``[overlays.t3-teatree] identity_aliases`` — applied at runtime via
# ``OverlayConfig.apply_toml_overrides`` — so they stay out of this public
# repo. The first group lists the public GitHub login alone.
IDENTITY_ALIASES: list[list[str]] = [
    ["souliane"],
]

# ── Workflow ────────────────────────────────────────────────────────

REQUIRE_TICKET: bool = True

# Default = close-on-merge: a merged teatree PR systematically closes its
# referenced issue. Suppression is the exception, opted into per-PR via
# ``Ticket.extra['more_prs_coming']`` (a declared partial, or an umbrella
# with remaining tracked scope). See ``should_close_ticket``.
MR_CLOSE_TICKET: bool = True

# Dogfooding overlay raises loop auto-start concurrency above the
# conservative base default of 1 (external/multi-repo overlays keep 1).
MAX_CONCURRENT_AUTO_STARTS: int = 3
