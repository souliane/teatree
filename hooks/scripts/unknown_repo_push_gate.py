"""PreToolUse: block-unknown-repo-push (SCOPE gate).

A ``git push`` carries commits to a remote. When a registered overlay has
declared its working SCOPE (``owned_repos``), a push to a repo NO overlay
claims is UNKNOWN — possibly a mis-targeted remote or a repo the operator
never meant the agent to touch. This gate holds such a push for operator
approval, the same posture the on-behalf gate takes for a colleague-visible
post it cannot self-authorise.

SCOPE is orthogonal to VISIBILITY (private_repos / leak-prevention) and to
COLLABORATION (the author/review gate): an owned private repo pushes freely,
an owned shared repo still needs review at merge time — this gate touches
NEITHER of those decisions, only owned-vs-unknown.

OPT-IN + never-lockout: it fires only when some overlay declared
``owned_repos`` (an install with none sees no new gate); a per-call
``[scope-push-ok: <reason>]`` token and the ``unknown_repo_push_gate_enabled``
kill-switch both ALLOW; an unresolvable cwd/slug fails OPEN; and the deny
routes through ``_fail_open_or_deny`` so the self-rescue allowlist + master
fail-open switch + circuit breaker all apply.

``bootstrap_teatree_django`` comes from the shared ``django_bootstrap`` leaf
(no dependency on ``hook_router``, so it imports at top level). The remaining
helpers that resolve overlays and emit the deny (``_resolve_cwd_repo``,
``_fail_open_or_deny``, ``_teatree_bool_setting``) live in ``hook_router`` and
are imported lazily at call time — ``hook_router`` imports this module at top
level, so importing it back at top level here would be a cycle.
"""

import re
import sys
from pathlib import Path

from hooks.scripts.django_bootstrap import bootstrap_teatree_django

# Alias the bare and ``hooks.scripts.`` identities so the handler the router
# registers and a test patching a helper here operate on ONE module object.
sys.modules.setdefault("unknown_repo_push_gate", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.unknown_repo_push_gate", sys.modules[__name__])

# A real push to a remote — not a local/query form. ``--dry-run`` (and ``-n``)
# perform no write, so they are exempt. A bare ``git push`` (no remote arg)
# still pushes the current branch, so the remote token is not required.
_SCOPE_GIT_PUSH_RE = re.compile(r"\bgit\s+push\b")
_SCOPE_GIT_PUSH_DRY_RUN_RE = re.compile(r"\bgit\s+push\b[^\n|;&]*?(?:--dry-run|\s-n\b)")
_SCOPE_PUSH_OK_RE = re.compile(r"\[scope-push-ok:\s*(\S[^\]]*?)\s*\]")


def _unknown_repo_push_gate_enabled() -> bool:
    """Whether the unknown-repo push SCOPE gate is enabled (default True).

    Fails OPEN to enabled on a missing/broken config; an explicit ``false``
    (``[teatree] unknown_repo_push_gate_enabled = false``) is the one-line
    kill-switch. The gate is ALSO inert whenever no overlay declared
    ``owned_repos``, so this switch only matters once an overlay opts in.
    """
    from hooks.scripts.hook_router import _teatree_bool_setting  # noqa: PLC0415 deferred back-import

    return _teatree_bool_setting("unknown_repo_push_gate_enabled", default=True)


def _scope_push_ok_token(command: str) -> str | None:
    """Return the reason from a ``[scope-push-ok: <reason>]`` token, else None.

    Scanned within the first 512 chars of the Bash command (mirroring the
    other per-call escapes); an empty reason returns None.
    """
    match = _SCOPE_PUSH_OK_RE.search(command[:512])
    if not match:
        return None
    reason = match.group(1).strip()
    return reason or None


def _classify_push_for_cwd(cwd: Path) -> str:
    """Classify a push from *cwd* against the live overlays: ``allow``/``require_approval``.

    Bootstraps Django first — ``classify_active_push`` resolves overlays via
    ``get_all_overlays()`` which trips the app registry, and the hook subprocess
    never calls ``django.setup()`` on its own. Without the bootstrap every push
    classified ``allow`` (the registry error was swallowed below), so the gate
    was production-dead. The caller (:func:`_unknown_repo_push_is_in_scope`)
    already short-circuits on the cheap kill-switch BEFORE reaching here, so the
    common case (gate disabled — the shipped default) never pays the bootstrap.

    Reuses the forge-host-keyed classifier
    :func:`teatree.core.gates.owned_repo_guard.classify_active_push`. Any
    import/resolution EXCEPTION (incl. a failed bootstrap) fails OPEN to
    ``allow`` (never-lockout on the internal-exception axis) — distinct from a
    clean ``require_approval`` verdict, which holds the push.
    """
    if not bootstrap_teatree_django():
        return "allow"
    try:
        from teatree.core.gates.owned_repo_guard import classify_active_push  # noqa: PLC0415

        return str(classify_active_push(cwd))
    except Exception:  # noqa: BLE001 — fail OPEN; a broken resolver must not wedge a push.
        return "allow"


_UNKNOWN_REPO_PUSH_REASON = (
    "HELD FOR APPROVAL: `git push` targets a repo OUTSIDE every registered "
    "overlay's declared working scope (`owned_repos`). This is the SCOPE axis — "
    "owned-vs-unknown — and is separate from visibility (private_repos) and from "
    "review (the author/merge gate). The agent does not push to a repo no overlay "
    "claims without the operator's go-ahead: confirm with the user, or add this "
    "repo's host/namespace to the overlay's `owned_repos`. If this is a vetted "
    "one-off, append `[scope-push-ok: <reason>]` to the command."
)


def _unknown_repo_push_is_in_scope(data: dict) -> bool:
    """Whether the call is a real ``git push`` this gate must evaluate.

    True only for a ``Bash`` ``git push`` that writes (``--dry-run`` / ``-n``
    are exempt), with the gate enabled and no per-call
    ``[scope-push-ok: <reason>]`` token present. A present token is honoured
    here (with a stderr NOTE) so the handler stays a single decision.
    """
    if data.get("tool_name") != "Bash":
        return False
    command = data.get("tool_input", {}).get("command", "")
    if not command or not _SCOPE_GIT_PUSH_RE.search(command) or _SCOPE_GIT_PUSH_DRY_RUN_RE.search(command):
        return False
    if not _unknown_repo_push_gate_enabled():
        return False
    if reason_token := _scope_push_ok_token(command):
        sys.stderr.write(f"NOTE: unknown-repo push gate skipped via [scope-push-ok: {reason_token}].\n")
        return False
    return True


def handle_block_unknown_repo_push(data: dict) -> bool:
    """Hold a ``git push`` to an UNKNOWN (no overlay owns it) repo for approval.

    Fires only when ALL hold (see :func:`_unknown_repo_push_is_in_scope` for
    the in-scope pre-checks):

    1. the tool is ``Bash`` running a real ``git push`` (``--dry-run`` /
        ``-n`` are exempt — they write nothing);
    2. the gate is enabled (``[teatree] unknown_repo_push_gate_enabled``,
        default True) and no per-call ``[scope-push-ok: <reason>]`` token is
        present;
    3. the cwd resolves to a directory, and
    4. the cwd repo is classified ``require_approval`` — i.e. some overlay
        opted into scope gating yet NO overlay owns its ``(host, namespace)``.

    Every other case ALLOWS: a non-push command, a dry-run, an unresolvable
    cwd, no opted-in overlay, or a repo some overlay owns. The deny routes
    through :func:`_fail_open_or_deny` so the self-rescue allowlist + master
    fail-open switch + circuit breaker all apply (never-lockout).
    """
    from hooks.scripts.hook_router import _fail_open_or_deny, _resolve_cwd_repo  # noqa: PLC0415 deferred back-import

    if not _unknown_repo_push_is_in_scope(data):
        return False
    cwd = _resolve_cwd_repo(data)
    if cwd is None:
        return False
    if _classify_push_for_cwd(cwd) != "require_approval":
        return False
    return _fail_open_or_deny(data, _UNKNOWN_REPO_PUSH_REASON)
