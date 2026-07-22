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
# private handles belong in the DB overlays registry, under the
# ``t3-teatree`` entry's ``identity_aliases`` field — applied at runtime by
# the per-overlay override step — so they stay out of this public repo. The
# first group lists the public GitHub login alone.
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
# belong in this public overlay's scope. A ``t3-teatree`` ``owned_repos`` value in
# the DB overlays registry REPLACES this dict (authoritative-and-complete), so the
# operator adds any extra owned host/namespace there, out of the public repo.
OWNED_REPOS: dict[str, list[str]] = {
    "github.com": ["souliane"],
}

# The unknown-repo approval gate ships INERT (opt-in, default off). Enabling it
# requires FIRST declaring the FULL owned host/namespace list — including every
# private/customer forge the operator merges on — because the gate fails CLOSED
# on any repo no listed pattern owns. The public OWNED_REPOS above is scoped to
# github.com/souliane only, so flipping this True here would hold the operator's
# own private-forge keystone merges as "unknown". A path-only registry overlay also
# cannot carry its own scope (it has no class, so overlay discovery skips it), so
# its repos must be declared under THIS overlay's owned_repos. The operator opts in
# from the DB overlays registry — the ``t3-teatree`` entry's ``owned_repos`` (full
# host list) + ``require_owned_repo_approval = true`` — where brand strings are
# allowed and never reach this public repo.
REQUIRE_OWNED_REPO_APPROVAL: bool = False

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

# ── Third-party services wrapped as MCP tool groups ────────────────

# The services this overlay's work actually touches: the GitHub forge
# (souliane/* repos) and Slack (notify DMs, review broadcasts). The MCP
# server registers a service's tool group only when a registered overlay
# declares it here (or via the ``overlays`` DB registry row override).
REQUIRED_THIRD_PARTY_SERVICES: list[str] = ["github", "slack"]

# ── Review ──────────────────────────────────────────────────────────

# #36: the per-ticket deep-review skill, promoted from a DB row to this public
# overlay code default (the value is already the public ``architectural_review_skill``
# default). The #1539 reviewing-phase evidence gate resolves ``review_skill``
# through env -> DB(overlay) -> DB(global) -> THIS code default -> the "" dataclass
# default, so a ``ConfigSetting`` row still overrides it and the DB stays the home
# for any per-machine change.
REVIEW_SKILL: str = "ac-reviewing-codebase"

# ── Issue intake ────────────────────────────────────────────────────

# #3573: this is the owner's own dogfood repo, so the factory auto-works EVERY
# issue. Promoted from a DB row to this public overlay code default (the value is
# public and constant); a colleague overlay keeps the strict ``ASSIGNED_AND_LABELED``
# dataclass default. ``admit_issue`` resolves ``admission_policy`` through
# env -> DB(overlay) -> DB(global) -> THIS code default -> the dataclass default,
# so a ``ConfigSetting`` row still overrides it.
ADMISSION_POLICY: str = "all"

# ── Companion skills ────────────────────────────────────────────────

# Skills loaded alongside the active lifecycle skill for every task in this
# overlay. Teatree posts structured output to Slack (notify DMs, loop digests),
# so the slack-formatting reference rides along so a dispatched worker builds
# native table blocks + fence fallbacks rather than hand-rolled pipe tables.
COMPANION_SKILLS: list[str] = ["slack-formatting"]
