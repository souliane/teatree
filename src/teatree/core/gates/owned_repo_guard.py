"""Unknown-repo SCOPE gate — opt-in, fail-CLOSED on a clean "unknown" verdict.

A push or merge to a repo whose ``(forge-host, namespace)`` falls within NO
overlay-declared scope (``owned_repos``) is the one action the agent should
pause on: it may be a mis-targeted remote or a repo the operator never meant
the agent to touch. This gate holds such an action for the operator.

Polarity (the load-bearing distinction):

*   A clean **"unknown" VERDICT** (the repo is identifiable but outside scope)
    fails **CLOSED** — block/ask. This is the OPPOSITE of the VISIBILITY gate
    (``teatree.hooks.publish_destination``), which fails OPEN: an unknown
    repo there is treated as not-private. Ownership-unknown → ask; visibility-
    unknown → scan-as-public. Never reuse the visibility verdict.
*   A gate **internal EXCEPTION** (a resolver bug, an unimportable dependency)
    is a separate concern handled by the *caller* / hook wrapper, which fails
    OPEN (never-lockout) — a gate bug must never brick every push.

Opt-in + misconfig guard:

*   ``require_owned_repo_approval`` False (default) → pass. Unmodified overlays
    never see this gate.
*   ``owned_repos`` empty under the flag → pass (misconfig guard: a flag set
    with no declared scope must not block EVERYTHING).
*   ``approved`` (per-invocation ``--approve-unowned`` / interactive
    AskUserQuestion) → pass. Approval is per-invocation, never persisted.
*   verdict ``unknown`` → raise :class:`UnownedRepoError` (block-with-remediation).
"""

from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypedDict

from teatree.core.intake.repo_scope import (
    host_aware_owns,
    identity_from_host_and_slug,
    repo_identity_for_cwd,
    repo_scope,
)

if TYPE_CHECKING:
    from teatree.core.overlay import OverlayBase


class MergeKeystoneResult(TypedDict, total=False):
    """The §17.4 keystone-merge result (success, escalation, or precondition refusal)."""

    merged: bool
    pr_id: int
    slug: str
    merged_sha: str
    ticket_id: int
    ticket_state: str
    error: str
    escalated: bool
    # ``"substrate"`` when a substrate-class refusal was escalated (the loop edge
    # pings the owner once and holds — ping-and-hold). Empty / absent for any
    # other refusal so the loop pings ONLY on substrate, not every held merge.
    escalation_kind: str
    # #3413: the config-sourced standing substrate authorizer id when the merge was
    # authorized by the owner's standing delegation (``substrate_auto_merge_authorized_by``);
    # absent for every other merge, so the loop edge posts the "informed, not asked"
    # Slack notification only when present.
    standing_delegation_by: str


class _MergeClearLike(Protocol):
    slug: str
    pr_id: int | str
    ticket: object

    def is_substrate(self) -> bool: ...  # pragma: no branch


class PushScopeVerdict(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"


class UnownedRepoError(RuntimeError):
    """Raised when a push/merge targets a repo outside the overlay's declared scope.

    Carries an actionable message naming the remediation (declare the
    host/namespace in ``[overlays.<name>.owned_repos]`` or pass the
    per-invocation approval) so the caller surfaces a satisfiable block,
    never a silent proceed.
    """


def _remediation(target: str) -> str:
    return (
        f"HELD FOR APPROVAL: target `{target}` is OUTSIDE every declared "
        "working scope (`owned_repos`). This is the SCOPE axis — owned-vs-unknown — "
        "separate from visibility (private_repos) and from review (the author/merge "
        "gate). Add its host/namespace to `[overlays.<name>.owned_repos]` "
        '(e.g. `github.com = ["<owner>"]`), or approve this one push '
        "(`--approve-unowned`, or confirm when asked)."
    )


def require_owned_or_approved(cwd: Path, overlay: "OverlayBase", *, approved: bool = False) -> None:
    """Hold an out-of-scope push/merge for approval; pass everything else.

    Fail-CLOSED on a clean ``unknown`` verdict. The opt-in flag and the
    empty-``owned_repos`` misconfig guard both PASS, as does a per-invocation
    *approved*. A resolver EXCEPTION is NOT caught here — the caller's
    never-lockout wrapper owns fail-open-on-exception so this gate stays a
    pure verdict.
    """
    if not overlay.config.require_owned_repo_approval:
        return
    if not overlay.config.owned_repos:
        return
    if approved:
        return
    if repo_scope(cwd, overlay.config.owned_repos) == "unknown":
        identity = repo_identity_for_cwd(cwd)
        target = f"{identity.host}/{identity.namespace}" if identity.host else (identity.namespace or "<unresolved>")
        raise UnownedRepoError(_remediation(target))


def _opted_in_scopes(
    overlays: dict[str, "OverlayBase"],
    path_only_scopes: "list[dict[str, list[str]]] | None",
) -> list[dict[str, list[str]]]:
    """The opted-in ``owned_repos`` of instantiable AND path-only overlays.

    A path-only TOML overlay is skipped by ``get_all_overlays()`` so its scope
    never reaches *overlays*; its opted-in ``owned_repos`` arrive separately
    via *path_only_scopes* (already filtered to opted-in + non-empty). Both are
    pooled into one list of scope dicts the verdict functions match against.
    """
    instantiable = [
        overlay.config.owned_repos
        for overlay in overlays.values()
        if overlay.config.require_owned_repo_approval and overlay.config.owned_repos
    ]
    return instantiable + list(path_only_scopes or [])


def classify_push_for_overlays(
    cwd: Path,
    overlays: dict[str, "OverlayBase"],
    *,
    path_only_scopes: "list[dict[str, list[str]]] | None" = None,
) -> PushScopeVerdict:
    """Classify a push from *cwd* against every registered overlay's scope.

    Mirrors the §7 verdict structure for the multi-overlay PreToolUse push
    gate (a push has no single active overlay). Returns
    :attr:`PushScopeVerdict.REQUIRE_APPROVAL` only when ALL hold:

    *   at least one registered overlay opted in
        (``require_owned_repo_approval`` AND non-empty ``owned_repos``), AND
    *   NO opted-in overlay owns the cwd repo's ``(host, namespace)``.

    *path_only_scopes* carries the opted-in ``owned_repos`` of PATH-ONLY
    overlays (``path`` but no ``class``), which ``get_all_overlays()`` skips —
    so a repo a path-only overlay owns is in scope. Otherwise
    :attr:`PushScopeVerdict.ALLOW`: no overlay opted in (back-compat), or some
    opted-in overlay owns the repo. The polarity is fail-CLOSED on the clean
    unknown verdict; a resolver EXCEPTION is the caller's fail-open concern,
    not this pure function's.
    """
    scopes = _opted_in_scopes(overlays, path_only_scopes)
    if not scopes:
        return PushScopeVerdict.ALLOW
    identity = repo_identity_for_cwd(cwd)
    if any(host_aware_owns(owned, identity) for owned in scopes):
        return PushScopeVerdict.ALLOW
    return PushScopeVerdict.REQUIRE_APPROVAL


def classify_active_push(cwd: Path) -> PushScopeVerdict:
    """Classify a push from *cwd* against the live registered overlays.

    Thin wrapper for the gate handler. Any discovery error fails OPEN
    (:attr:`PushScopeVerdict.ALLOW`) — a broken overlay must never wedge a push
    (never-lockout on the internal-exception axis, distinct from the clean
    unknown verdict above which fails closed).
    """
    from teatree.core.overlay_loader import OverlayConfigResolver, get_all_overlays  # noqa: PLC0415 — lazy import

    try:
        overlays = get_all_overlays()
        path_only_scopes = OverlayConfigResolver.path_only_owned_scopes()
    except Exception:  # noqa: BLE001 — broken overlay must not wedge a push; fail OPEN.
        return PushScopeVerdict.ALLOW
    return classify_push_for_overlays(cwd, overlays, path_only_scopes=path_only_scopes)


def merge_scope_verdict(
    host: str,
    slug: str,
    overlays: dict[str, "OverlayBase"],
    *,
    path_only_scopes: "list[dict[str, list[str]]] | None" = None,
) -> PushScopeVerdict:
    """Classify a keystone-merge target ``(host, slug)`` against every overlay's scope.

    The merge keystone carries the namespace as a bare ``slug`` (no host); the
    *host* is recovered from the ticket's issue/PR URL by the caller. Same §7
    polarity as :func:`classify_push_for_overlays`: REQUIRE_APPROVAL only when
    some overlay opted in AND the target is identifiably out-of-scope (host
    known, namespace owned by no opted-in overlay). ALLOW otherwise.

    *path_only_scopes* carries the opted-in ``owned_repos`` of PATH-ONLY
    overlays (skipped by ``get_all_overlays()``), so a merge target a path-only
    overlay owns is in scope.

    An UNRESOLVABLE host (no issue/PR URL to recover it from) is the
    *uncertainty* axis, NOT a clean unknown verdict — the merge cannot be
    classified, so it fails OPEN (never-lockout), exactly as the push gate
    allows an unresolvable cwd. The merge keystone's other gates (independent
    cold-review, SHA-binding, substrate authorization) remain in force.
    """
    scopes = _opted_in_scopes(overlays, path_only_scopes)
    if not scopes:
        return PushScopeVerdict.ALLOW
    identity = identity_from_host_and_slug(host, slug)
    if not identity.host:
        return PushScopeVerdict.ALLOW
    if any(host_aware_owns(owned, identity) for owned in scopes):
        return PushScopeVerdict.ALLOW
    return PushScopeVerdict.REQUIRE_APPROVAL


def escalated_merge_result(clear: "_MergeClearLike", error: str) -> MergeKeystoneResult:
    """The canonical "merge held, re-escalate" result for a CLEAR.

    Shared by every keystone-merge refusal (scope-gate AND merge-precondition)
    so the held-merge result shape stays defined in exactly one place. Carries
    ``escalation_kind = "substrate"`` when the held CLEAR is substrate, so the
    loop edge pings the owner ONCE and holds (ping-and-hold) — and only on
    substrate, never on every held merge.
    """
    return {
        "merged": False,
        "escalated": True,
        "pr_id": int(clear.pr_id),
        "slug": clear.slug,
        "error": error,
        "escalation_kind": "substrate" if clear.is_substrate() else "",
    }


def merge_clear_refusal(clear: "_MergeClearLike", *, approved: bool) -> MergeKeystoneResult | None:
    """Escalation result when a keystone merge targets a repo outside every overlay's scope.

    The keystone-merge chokepoint counterpart of the push gate: a CLEAR whose
    ``(host, slug)`` no opted-in overlay owns is held for the operator —
    returning a ready-made escalation result (``merged`` False, ``escalated``
    True, the actionable ``error``), FSM untouched — unless a per-invocation
    human authorization is re-presented. The host is recovered from the
    ticket's ``issue_url`` (the keystone slug is host-less). Returns ``None``
    (proceed) when *approved*, in scope, or no overlay opted in; a resolution
    EXCEPTION also returns ``None`` (fail OPEN — never-lockout).
    """
    if approved:
        return None
    try:
        from teatree.core.overlay_loader import OverlayConfigResolver, get_all_overlays  # noqa: PLC0415 — lazy import

        issue_url = str(getattr(clear.ticket, "issue_url", "") or "")
        verdict = merge_scope_verdict(
            issue_url,
            clear.slug,
            get_all_overlays(),
            path_only_scopes=OverlayConfigResolver.path_only_owned_scopes(),
        )
    except Exception:  # noqa: BLE001 — a resolver error must never wedge a merge; fail OPEN.
        return None
    if verdict is not PushScopeVerdict.REQUIRE_APPROVAL:
        return None
    return escalated_merge_result(
        clear,
        f"merge target `{clear.slug}` (#{clear.pr_id}) is OUTSIDE every overlay's declared "
        "working scope (`owned_repos`). The keystone holds an out-of-scope merge for the "
        "operator: add its host/namespace to `[overlays.<name>.owned_repos]`, or re-present "
        "the human authorization (`--human-authorized <id>`).",
    )
