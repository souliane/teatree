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

# ── Scope (which repos this overlay legitimately works on) ──────────

# The SCOPE axis (distinct from VISIBILITY / ``private_repos`` and from
# COLLABORATION / the author-review gate). Forge-host-keyed: only github.com
# repos are in this overlay's scope, so any gitlab.com repo is UNKNOWN here.
# ``"souliane"`` is a namespace-prefix wildcard covering souliane/teatree AND
# every other souliane repo (no enumeration). A downstream overlay declares its
# own private/customer namespaces in ITS OWN repo's ``OWNED_REPOS`` — they never
# belong in this public overlay's scope. A ``[overlays.t3-teatree.owned_repos]``
# TOML table REPLACES this dict (authoritative-and-complete), so the operator
# adds any extra owned host/namespace there, out of the public repo.
OWNED_REPOS: dict[str, list[str]] = {
    "github.com": ["souliane"],
}

# Activate the unknown-repo approval gate for the dogfood overlay: a push to a
# repo outside OWNED_REPOS is held for the operator (the gate is opt-in, so it
# stays inert for every overlay that does not set this).
REQUIRE_OWNED_REPO_APPROVAL: bool = True

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
