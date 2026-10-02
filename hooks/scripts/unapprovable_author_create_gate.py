"""PreToolUse: refuse a raw MR/PR create on a repo that declares a non-owner author.

A repo may declare that its MRs are authored under a NON-OWNER credential, so the human
owner stays eligible to approve them. Only a teatree create path consults that declaration
(``authoring_credential.gitlab_token_for_remote``); a raw ``glab mr create`` / ``gh pr
create`` writes under whatever credential the forge CLI itself carries — the owner's — and a
forge bars an author from approving their own MR. GitLab answers that refusal with **HTTP
401, not 403**, so the defect surfaces days later as an indistinguishable dead-token error
and sends the reader chasing credentials. Three MRs had to be closed and re-created in one
day before this gate existed.

Deliberately narrower in SCOPE than the sibling out-of-band-MERGE gate. That gate is right to
block on repo MANAGED-ness — an out-of-band merge bypasses the FSM on any managed repo
regardless of who authors it. A raw create's defect is identity-specific: it only produces an
unapprovable MR on a repo that DECLARES a non-owner author
(:func:`teatree.core.authoring_credential.declared_distinct_author`). A managed repo with no
such declaration (teatree's own upstream, ``souliane/teatree``, chief among them) is authored
by the owner regardless of which surface opens the MR, so a raw create there is not this
gate's business — blocking it there denies the documented upstream-contribution flow for a
defect that provably cannot occur. Detection is the pure :mod:`teatree.hooks.raw_create_detect`
leaf; the scope is decided by the tri-state TARGET classification — a target that DECLARES a
distinct author BLOCKS regardless of cwd, a target that parses and declares none ALLOWS on its
own evidence, and only a target that does not parse consults the cwd (fail-safe to BLOCK when
the cwd cannot be classified either).

Never-lockout: the ``raw_pr_create_gate_enabled`` kill-switch
(``t3 <overlay> gate raw-pr-create disable``) ALLOWS, the deny routes through
``_fail_open_or_deny`` so the self-rescue allowlist + master fail-open switch + circuit
breaker all apply, and both sanctioned paths are named in the refusal — including the
ticketless ``pr ensure-pr``, whose absence from the refusal is what sends the next agent
back to the raw command.

Cold-import safe: stdlib-only module top plus the already-extracted ``mr_cli_fields`` /
``managed_repo`` siblings. The router imports this module at top level, so the helpers that
emit the deny, read config, and resolve the distinct-author declaration are back-imported
lazily at call time.
"""

import sys
from pathlib import Path

from hooks.scripts.managed_repo import teatree_src_on_path

# Alias the bare and ``hooks.scripts.`` identities so the handler the router registers and a
# test patching a helper here operate on ONE module object.
sys.modules.setdefault("unapprovable_author_create_gate", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.unapprovable_author_create_gate", sys.modules[__name__])

GATE_SETTING = "raw_pr_create_gate_enabled"

_DENY_PREFIX = "BLOCKED: raw MR/PR create on a repo requiring a non-owner author — "
_DENY_SUFFIX = " kill-switch: `t3 <overlay> gate raw-pr-create disable`."

# Characters a target only carries when the shell still has to expand it, so it names no repo offline.
_DYNAMIC_TARGET_CHARS = frozenset("$`\"'(){}*?")


def _gate_enabled() -> bool:
    """Whether the gate is enabled (default True); an explicit ``false`` is the kill-switch."""
    from hooks.scripts.hook_router import _teatree_bool_setting  # noqa: PLC0415 deferred back-import

    return _teatree_bool_setting(GATE_SETTING, default=True)


def raw_create_shape_reason(command: str) -> str | None:
    """The shared leaf's raw-create reason for *command*, or ``None`` when it is not one.

    Fails CLOSED on any import error (a generic reason, so the command is still classified as
    a create) — a broken environment must not weaken the gate; the target check then decides.
    """
    try:
        with teatree_src_on_path():
            from teatree.hooks import raw_create_detect  # noqa: PLC0415 — deferred: cold-hook import

            return raw_create_detect.raw_create_deny_reason(command)
    except Exception:  # noqa: BLE001 — fail-closed: a broken import must not weaken the create gate
        return _DENY_PREFIX


def _declares_distinct_author(slug_or_remote: str) -> bool:
    """Whether a REGISTERED overlay routes *slug_or_remote* to a non-owner author — resolvable or not.

    An overlay whose scoped credential does not resolve here (``unreachable_in``) still declares
    the repo bot-authored; the raw CLI would then write as the owner and open exactly the MR this
    gate exists to prevent, so that counts as a declaration too. Resolution is by SLUG
    (``slug_from_remote`` is idempotent on a bare ``owner/repo``). Fails CLOSED on any error (an
    unbootstrappable Django, an unregistered overlay, two overlays disagreeing) — a broken venue
    must not weaken the gate.
    """
    try:
        with teatree_src_on_path():
            from teatree.core.authoring_credential import (  # noqa: PLC0415 — deferred: cold-hook import
                overlay_authoring_for,
            )

            authoring = overlay_authoring_for(slug_or_remote)
            return authoring.declared is not None or bool(authoring.unreachable_in)
    except Exception:  # noqa: BLE001 — fail-closed: a broken resolution must not weaken the gate
        return True


def _cwd_remote(cwd: Path) -> str | None:
    """A synthetic ``https://<host>/<owner>/<repo>`` remote URL for *cwd*, or ``None``.

    ``publish_surface.slug_for_cwd`` resolves OFFLINE (parses ``.git/config`` directly, no
    ``git`` binary needed inside the restricted PreToolUse subprocess) but returns a
    HOST-QUALIFIED slug (``host/owner/repo``) — a shape ``slug_from_remote`` does not strip
    (its host-prefix regexes all require a scheme or ``git@``), so passing it to
    ``declared_distinct_author`` as-is would never match a declared slug. Re-prefixing with
    ``https://`` puts it back through the SAME normalization the ``-R``/``--repo`` flag path
    already relies on, rather than teaching a second, divergent slug comparison.
    """
    try:
        with teatree_src_on_path():
            from teatree.hooks import publish_surface  # noqa: PLC0415 — deferred: cold-hook import

            slug = publish_surface.slug_for_cwd(cwd)
    except Exception:  # noqa: BLE001 — crash-proof: an unresolvable cwd is handled by the caller
        return None
    return f"https://{slug}" if slug else None


def _raw_create_targets(command: str) -> list[str | None]:
    """Each executed create's own target; ``[None]`` (judge by the cwd) when they cannot be read."""
    try:
        with teatree_src_on_path():
            from teatree.hooks import raw_create_detect  # noqa: PLC0415 — deferred: cold-hook import

            return raw_create_detect.raw_create_targets(command) or [None]
    except Exception:  # noqa: BLE001 — fail-closed: an unreadable target is judged by the cwd
        return [None]


def _classifiable(target: str | None) -> str | None:
    """*target* when it names an ``owner/repo`` offline, else ``None``.

    A numeric project id (``projects/9``) or a shell-expanded one (``--repo "$REPO"``) could be
    any repo, a distinct-author one included, so neither is evidence on its own.
    """
    if target is None or "/" not in target or _DYNAMIC_TARGET_CHARS & set(target):
        return None
    return target


def _target_declares_distinct_author(command: str, cwd: Path | None) -> bool:
    """Whether ANY executed create targets a distinct-author repo, each judged on its own argv.

    Mirrors the out-of-band-merge gate's SHAPE, not its predicate: a classifiable target answers
    on its own evidence (declares a distinct author → BLOCK; a real ``owner/repo`` that declares
    none → ALLOW, regardless of cwd), and an absent, opaque or dynamic target defers to the cwd
    — where an unresolvable remote counts as requiring the block (fail-safe).
    """
    return any(_one_target_declares_distinct_author(target, cwd) for target in _raw_create_targets(command))


def _one_target_declares_distinct_author(target: str | None, cwd: Path | None) -> bool:
    if (classifiable := _classifiable(target)) is not None:
        return _declares_distinct_author(classifiable)
    if cwd is None:
        return True
    cwd_remote = _cwd_remote(cwd)
    if cwd_remote is None:
        return True
    return _declares_distinct_author(cwd_remote)


def handle_block_unapprovable_author_create(data: dict) -> bool:
    """Block a raw ``glab mr create`` / ``gh pr create`` / REST MR-create on a distinct-author repo.

    Fires only when the gate is enabled, the command really is a raw create (action-aware —
    never a heredoc body, quoted operand or ``#`` comment), and the target repo declares a
    distinct author. Everything else ALLOWS — including a managed repo (teatree's own
    upstream among them) that declares no such credential, since a raw create there cannot
    produce an unapprovable MR.
    """
    from hooks.scripts.hook_router import _fail_open_or_deny, _resolve_cwd_repo  # noqa: PLC0415 deferred back-import

    if data.get("tool_name") != "Bash" or not _gate_enabled():
        return False
    command = data.get("tool_input", {}).get("command", "")
    if not command:
        return False
    shape_reason = raw_create_shape_reason(command)
    if shape_reason is None:
        return False
    if not _target_declares_distinct_author(command, _resolve_cwd_repo(data)):
        return False
    return _fail_open_or_deny(data, f"{_DENY_PREFIX}{shape_reason}{_DENY_SUFFIX}")
