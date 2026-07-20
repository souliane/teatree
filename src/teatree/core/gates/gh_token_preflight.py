"""GitHub token permission preflight (#3405, expanded #3477).

A token that authenticates but lacks a permission the loop needs fails LATE,
mid-run, with "Resource not accessible". This probes the effective permission
set up front. Never-lockout invariant: only :data:`REQUIRED_PERMISSION_LABELS`
(unchanged 4 from #3405) can fail deploy/doctor; every permission added since
is :data:`RECOMMENDED_PERMISSION_LABELS` — WARN + remediation only, never a
hard failure.

Fine-grained PAT: each permission gets a side-effect-free probe against a
resource that never exists — 403 "not accessible" = denied, 404/200 = present
(a read probe's 404/5xx/network miss is an indeterminate skip, never
"missing"). Classic PAT: the 403 probe fails open, so it's judged by
``X-OAuth-Scopes`` membership instead (``repo`` required; ``workflow``/
``read:project`` recommended — the rest is bundled into ``repo``).

``workflows: write`` is never actively probed on a fine-grained token — the
#3477 spike could only confirm the permitted (404) path, not whether a denied
token 403s route-level first, so probing risks a false "missing". Always
reported as an unprobed WARN gap instead.

``projects: read`` is probed only when ``github_owner`` + ``github_project_number``
are supplied (an unconfigured board is never assessed).

GitHub has no API to widen a token's grant, so :func:`format_remediation`
only ever proposes a recreate.
"""

import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from teatree.utils.run import run_allowed_to_fail

# Never-lockout pinning contract — exactly these four, pinned to deploy/entrypoint.sh by a test.
REQUIRED_PERMISSION_LABELS: tuple[str, ...] = (
    "metadata: read",
    "issues: write",
    "pull_requests: write",
    "contents: write",
)

# WARN-tier only — a gap here never fails deploy/doctor.
RECOMMENDED_PERMISSION_LABELS: tuple[str, ...] = (
    "workflows: write",
    "actions: write",
    "actions: read",
    "checks: read",
    "statuses: read",
    "projects: read",
)

# One-line "what breaks without this" per permission, both tiers.
FEATURE_BY_PERMISSION: dict[str, str] = {
    "metadata: read": "reading the repo at all — every other probe short-circuits without it",
    "issues: write": "labelling/closing issues the loop manages",
    "pull_requests: write": "opening/merging PRs the loop manages",
    "contents: write": "pushing commits/branches the loop manages",
    "workflows: write": (
        "pushing a PR that touches .github/workflows/* (git-transport rejects it without this); "
        "UNPROBEABLE for a fine-grained token — verify manually"
    ),
    "actions: write": "`gh workflow run` dispatch (`t3 eval ci-trigger`)",
    "actions: read": "`gh run list`/`view`/`download` (`t3 eval ci-status`)",
    "checks: read": (
        "the required-checks rollup auto-merge reads (forge_merge_rpc, self_update_ci) "
        "— strongly recommended: auto-merge fails closed without it"
    ),
    "statuses: read": "legacy commit-status rollup completeness alongside checks",
    "projects: read": "GitHub Projects v2 board sync (probed only when a board is configured)",
}

# Metadata-read denial signals — a permission/visibility fault, not a transient network one.
_DENIED_SIGNALS: tuple[str, ...] = (
    "not accessible",
    "not found",
    "bad credentials",
    "requires authentication",
    "must be authenticated",
)

# The route-level 403 denial signal for both write and read probes alike.
_FORBIDDEN_SIGNAL = "not accessible"

# GraphQL's denial shape (FORBIDDEN error type) — checked for the projects:read probe only.
_GRAPHQL_FORBIDDEN_SIGNAL = "forbidden"

# The classic-PAT scope granting write to issues/PRs/contents + read to actions/checks/statuses.
_CLASSIC_WRITE_SCOPE = "repo"

# Classic-PAT scopes for the two recommended perms NOT bundled into `repo` (label, scope).
_CLASSIC_RECOMMENDED_SCOPES: tuple[tuple[str, str], ...] = (
    ("workflows: write", "workflow"),
    ("projects: read", "read:project"),
)

# Presence signals a classic PAT; a fine-grained token omits this header.
_OAUTH_SCOPES_HEADER = "x-oauth-scopes"

type GhRunner = Callable[[list[str]], tuple[int, str]]

TokenKind = Literal["classic", "fine_grained", "unknown"]


@dataclass(frozen=True)
class Probe:
    """One side-effect-free permission probe (``gh api`` operand template + tier + kind)."""

    label: str
    tier: Literal["required", "recommended"]
    argv_template: tuple[str, ...]
    kind: Literal["read", "mutate"]


# `workflows: write` and `projects: read` are handled separately (see probe_token_permissions).
_PROBES: tuple[Probe, ...] = (
    Probe(
        "issues: write",
        "required",
        ("--method", "PATCH", "repos/{slug}/issues/0", "-f", "state=open"),
        "mutate",
    ),
    Probe(
        "pull_requests: write",
        "required",
        ("--method", "PATCH", "repos/{slug}/pulls/0", "-f", "state=open"),
        "mutate",
    ),
    Probe(
        "contents: write",
        "required",
        ("--method", "PATCH", "repos/{slug}/git/refs/heads/teatree-preflight-nonexistent"),
        "mutate",
    ),
    Probe(
        "actions: write",
        "recommended",
        (
            "--method",
            "POST",
            "repos/{slug}/actions/workflows/0/dispatches",
            "-f",
            "ref=teatree-preflight-nonexistent",
        ),
        "mutate",
    ),
    Probe("actions: read", "recommended", ("repos/{slug}/actions/artifacts?per_page=1",), "read"),
    Probe(
        "checks: read",
        "recommended",
        ("repos/{slug}/commits/{default_branch}/check-runs?per_page=1",),
        "read",
    ),
    Probe("statuses: read", "recommended", ("repos/{slug}/commits/{default_branch}/status",), "read"),
)

# Derived from _PROBES so the two never drift.
_WRITE_PERMISSION_LABELS: tuple[str, ...] = tuple(p.label for p in _PROBES if p.tier == "required")

# projects:read GraphQL query — a nonexistent project number is the permitted (NOT_FOUND) path.
_PROJECTS_QUERY_TEMPLATE = '{{user(login:"{owner}"){{projectV2(number:{number}){{id}}}}}}'

# GitHub has no API to widen a token's grant — both are "make a new one" links.
CLASSIC_TOKEN_RECREATE_URL = (
    "https://github.com/settings/tokens/new?scopes=repo,workflow,read:project&description=teatree"  # noqa: S105
)
FINE_GRAINED_TOKENS_URL = "https://github.com/settings/personal-access-tokens"


@dataclass(frozen=True)
class GhTokenProbe:
    """Outcome of a token-permission probe. ``missing_recommended`` never affects ``ok`` — WARN only."""

    missing: tuple[str, ...]
    missing_recommended: tuple[str, ...] = ()
    token_kind: TokenKind = "unknown"  # noqa: S105 — a classification label, not a credential
    indeterminate_reason: str | None = None

    @property
    def ok(self) -> bool:
        return not self.missing and self.indeterminate_reason is None


def _default_run(args: list[str]) -> tuple[int, str]:
    """Run ``gh api <args>``; a 4xx is an expected probe outcome, not an error to raise on."""
    result = run_allowed_to_fail(["gh", "api", *args], expected_codes=None)
    return result.returncode, f"{result.stdout}\n{result.stderr}"


def _has_signal(text: str, signals: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(signal in lowered for signal in signals)


def _oauth_scopes(headers_text: str) -> frozenset[str] | None:
    """Classic-PAT scopes from ``X-OAuth-Scopes``; ``None`` when absent (a fine-grained PAT)."""
    for line in headers_text.splitlines():
        name, sep, value = line.partition(":")
        if sep and name.strip().lower() == _OAUTH_SCOPES_HEADER:
            return frozenset(scope for scope in (s.strip() for s in value.split(",")) if scope)
    return None


def _parse_default_branch(meta_out: str) -> str | None:
    """Extract ``default_branch`` from the ``-i`` metadata read's JSON body; ``None`` if unparseable."""
    body = meta_out.rsplit("\n\n", 1)[-1]
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    branch = data.get("default_branch") if isinstance(data, dict) else None
    return branch if isinstance(branch, str) and branch else None


def _probe_verdict(run: GhRunner, probe: Probe, slug: str, default_branch: str | None) -> bool | None:
    """Run *probe*; ``True`` denied, ``False`` present, ``None`` skip (no resolvable default branch)."""
    template_text = " ".join(probe.argv_template)
    if "{default_branch}" in template_text and not default_branch:  # noqa: RUF027 — literal placeholder
        return None
    args = [part.format(slug=slug, default_branch=default_branch or "") for part in probe.argv_template]
    _code, out = run(args)
    return _FORBIDDEN_SIGNAL in out.lower()


def _projects_read_denied(run: GhRunner, owner: str, project_number: int) -> bool:
    """True when the fine-grained token's account-level ``projects: read`` is denied."""
    query = _PROJECTS_QUERY_TEMPLATE.format(owner=owner, number=project_number)
    _code, out = run(["graphql", "-f", f"query={query}"])
    return _has_signal(out, (_FORBIDDEN_SIGNAL, _GRAPHQL_FORBIDDEN_SIGNAL))


def probe_token_permissions(
    slug: str,
    run: GhRunner | None = None,
    *,
    github_owner: str = "",
    github_project_number: int = 0,
) -> GhTokenProbe:
    """Probe whether ``gh``'s token holds the required and recommended permissions on *slug*.

    ``github_owner`` + ``github_project_number`` gate the conditional
    ``projects: read`` probe. A metadata-read failure short-circuits: a denial
    signal reports ``missing=("metadata: read",)``, anything else is
    indeterminate. Token class then comes from the ``X-OAuth-Scopes`` header —
    classic is judged by scope, fine-grained by the per-permission probes.
    """
    run = run or _default_run
    if shutil.which("gh") is None:
        return GhTokenProbe(missing=(), indeterminate_reason="gh CLI not found on PATH")

    meta_code, meta_out = run(["-i", f"repos/{slug}"])
    if meta_code != 0:
        if _has_signal(meta_out, _DENIED_SIGNALS):
            return GhTokenProbe(missing=("metadata: read",))
        return GhTokenProbe(missing=(), indeterminate_reason=f"could not read repos/{slug} (API unreachable?)")

    scopes = _oauth_scopes(meta_out)
    if scopes is not None:
        # Classic PAT: the per-route 403 probe fails open for it — judge by scope membership instead.
        missing_required = () if _CLASSIC_WRITE_SCOPE in scopes else _WRITE_PERMISSION_LABELS
        classic_recommended_missing = {label for label, scope in _CLASSIC_RECOMMENDED_SCOPES if scope not in scopes}
        missing_recommended = tuple(
            label for label in RECOMMENDED_PERMISSION_LABELS if label in classic_recommended_missing
        )
        return GhTokenProbe(
            missing=missing_required,
            missing_recommended=missing_recommended,
            token_kind="classic",  # noqa: S106 — a classification label, not a credential
        )

    # Fine-grained PAT: per-permission route/read probes.
    default_branch = _parse_default_branch(meta_out)
    required_missing: set[str] = set()
    recommended_missing: set[str] = set()
    for probe in _PROBES:
        if _probe_verdict(run, probe, slug, default_branch):
            (required_missing if probe.tier == "required" else recommended_missing).add(probe.label)

    # Never actively probed (see module docstring) — always surfaced so remediation names it.
    recommended_missing.add("workflows: write")

    if github_owner and github_project_number and _projects_read_denied(run, github_owner, github_project_number):
        recommended_missing.add("projects: read")

    missing = tuple(label for label in REQUIRED_PERMISSION_LABELS if label in required_missing)
    missing_recommended = tuple(label for label in RECOMMENDED_PERMISSION_LABELS if label in recommended_missing)
    return GhTokenProbe(
        missing=missing,
        missing_recommended=missing_recommended,
        token_kind="fine_grained",  # noqa: S106 — a classification label, not a credential
    )


def format_remediation(probe: GhTokenProbe, slug: str) -> list[str]:
    """Remediation lines for every gap ``probe`` reports — always a recreate, never an auto-add. Pure/print-free."""
    missing_all = [*probe.missing, *probe.missing_recommended]
    if not missing_all:
        return []
    if probe.token_kind == "classic":  # noqa: S105 — a classification label, not a credential
        return [
            (
                f"TEATREE_GH_TOKEN (classic PAT) is missing {', '.join(missing_all)} on {slug}. "
                f"Classic tokens cannot be widened via the API — create a new one: {CLASSIC_TOKEN_RECREATE_URL}"
            )
        ]
    lines = [f"TEATREE_GH_TOKEN is missing the following permission(s) on {slug}:"]
    for label in missing_all:
        feature = FEATURE_BY_PERMISSION.get(label, "")
        lines.append(f"  {label} — needed for {feature}" if feature else f"  {label}")
    lines.append(
        "Fine-grained tokens cannot be widened via the API either — recreate it with these "
        f"permissions added: {FINE_GRAINED_TOKENS_URL}"
    )
    return lines


__all__ = [
    "CLASSIC_TOKEN_RECREATE_URL",
    "FEATURE_BY_PERMISSION",
    "FINE_GRAINED_TOKENS_URL",
    "RECOMMENDED_PERMISSION_LABELS",
    "REQUIRED_PERMISSION_LABELS",
    "GhRunner",
    "GhTokenProbe",
    "Probe",
    "TokenKind",
    "format_remediation",
    "probe_token_permissions",
]
