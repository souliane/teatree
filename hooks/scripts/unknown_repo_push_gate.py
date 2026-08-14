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
from hooks.scripts.gate_result import warn_gate_skipped
from hooks.scripts.managed_repo import teatree_src_on_path
from hooks.scripts.mr_cli_fields import strip_quoted_and_heredoc

# Alias the bare and ``hooks.scripts.`` identities so the handler the router
# registers and a test patching a helper here operate on ONE module object.
sys.modules.setdefault("unknown_repo_push_gate", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.unknown_repo_push_gate", sys.modules[__name__])

# A real push to a remote — not a local/query form. ``--dry-run`` (and ``-n``)
# perform no write, so they are exempt. A bare ``git push`` (no remote arg)
# still pushes the current branch, so the remote token is not required. The
# verb is anchored at a COMMAND position (start of the command or after a
# separator) and git's global flags may sit between ``git`` and ``push`` —
# ``git -C <dir> push`` is the form every sibling gate already handles, and
# requiring the two words adjacent let it through unevaluated.
_PUSH_PREFIX = r"(?:^|[;&|]\s*)(?:sudo\s+)?git\s+(?:(?:-C|-c|--git-dir|--work-tree|--namespace)(?:=\S+|\s+\S+)\s+)*"
_SCOPE_GIT_PUSH_RE = re.compile(_PUSH_PREFIX + r"push\b")
_SCOPE_GIT_PUSH_DRY_RUN_RE = re.compile(_PUSH_PREFIX + r"push\b[^\n|;&]*?(?:--dry-run|\s-n\b)")
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
    clean ``require_approval`` verdict, which holds the push. Each degraded
    ``allow`` says so on stderr (:func:`gate_result.warn_gate_skipped`): an
    unvalidated push and a scoped-and-cleared one are the same silence otherwise.
    """
    if not bootstrap_teatree_django():
        warn_gate_skipped("unknown-repo push SCOPE", "the hook interpreter cannot import Django")
        return "allow"
    try:
        from teatree.core.gates.owned_repo_guard import classify_active_push  # noqa: PLC0415 — cold-hook import

        return str(classify_active_push(cwd))
    except Exception as exc:  # noqa: BLE001 — fail OPEN; a broken resolver must not wedge a push.
        warn_gate_skipped("unknown-repo push SCOPE", f"the scope resolver failed ({type(exc).__name__}: {exc})")
        return "allow"


def _push_target_dir(command: str, cwd: Path | None) -> Path | None:
    """The dir whose repo this push LANDS in, or ``None`` when it cannot be pinned.

    Resolved by the canonical static resolver
    :func:`teatree.hooks._commit_repo_dir.resolve_commit_dir`, the same one
    ``main_clone_guard`` / ``single_branch_repo_guard`` /
    ``headless_authoring_gate`` use, so ``git -C <dir>``, ``cd <dir> &&`` and
    ``--git-dir`` all name the repo actually being pushed. Keying on the ambient
    ``cwd`` instead classified the SESSION's repo, and a session sits in one repo
    while pushing to another all day.

    ``None`` — the fail-OPEN answer — for an unresolvable target (the
    sentinel, no cwd, a resolver that cannot be imported): an unknown target is
    not evidence that a push leaves the operator's scope.
    """
    try:
        with teatree_src_on_path():
            from teatree.hooks._commit_repo_dir import (  # noqa: PLC0415, PLC2701 — cold-hook import
                UNRESOLVABLE_REPO_DIR,
                resolve_commit_dir,
            )

            landing = resolve_commit_dir(command, cwd)
        if landing == UNRESOLVABLE_REPO_DIR or not isinstance(landing, Path):
            return None
    except Exception:  # noqa: BLE001 — fail OPEN; an unimportable resolver must not wedge a push.
        return None
    return landing if landing.is_dir() else None


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

    The verb is matched against the quote/heredoc-stripped skeleton, as in every
    sibling gate: the same words inside a ``-m`` message or a heredoc body are
    text, not an invocation.
    """
    if data.get("tool_name") != "Bash":
        return False
    command = data.get("tool_input", {}).get("command", "")
    if not command:
        return False
    skeleton = strip_quoted_and_heredoc(command)
    if not _SCOPE_GIT_PUSH_RE.search(skeleton) or _SCOPE_GIT_PUSH_DRY_RUN_RE.search(skeleton):
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
    3. the push's LANDING dir resolves (:func:`_push_target_dir` — ``git -C``,
        a leading ``cd``, ``--git-dir``, else the ambient cwd), and
    4. that repo is classified ``require_approval`` — i.e. some overlay
        opted into scope gating yet NO overlay owns its ``(host, namespace)``.

    Every other case ALLOWS: a non-push command, a dry-run, an unresolvable
    target, no opted-in overlay, or a repo some overlay owns. The deny routes
    through :func:`_fail_open_or_deny` so the self-rescue allowlist + master
    fail-open switch + circuit breaker all apply (never-lockout).
    """
    from hooks.scripts.hook_router import _fail_open_or_deny, _resolve_cwd_repo  # noqa: PLC0415 deferred back-import

    if not _unknown_repo_push_is_in_scope(data):
        return False
    target = _push_target_dir(data.get("tool_input", {}).get("command", ""), _resolve_cwd_repo(data))
    if target is None:
        return False
    if _classify_push_for_cwd(target) != "require_approval":
        return False
    return _fail_open_or_deny(data, _UNKNOWN_REPO_PUSH_REASON)
