"""Block Bash commands that route a secret-bearing source to stdout (#2384 PR4).

The privacy scan gates COMMITS, but nothing gates a command that echoes a secret
to the transcript — once it lands there, rotation is the only remedy. This gate
denies a Bash command that would print a known secret-bearing source to stdout:

* cat/head/tail of known secret-bearing paths (credential files, key files,
    ``.env`` files, pass stores);
* ``pass show …`` whose stdout is NOT captured or redirected to a file;
* echo/printf of a pasted token literal (glpat-/ghp_/gho_/xoxb-/xoxp-/sk-).

Allowed (must NOT false-positive): reading a value into a shell variable
(``VAR=$(…)``), piping / redirecting to a file (``… > out.txt``), using the
value via env or header (``curl -H "Token: $VAR"``), cat of ordinary non-secret
files, and echo of prose that merely MENTIONS a secret-file path. Fails OPEN on
any internal error — a gate bug must never wedge the agent (consistent with the
#1164 raw-review-post guard).

The secret-print SHAPE detection lives in the ``teatree.hooks.secret_file_print_detect``
leaf (per-segment shell-lexed, so a redirect on an unrelated segment cannot mask
the leak and a print verb not at the whole-command start is still seen), lazily
imported inside the sibling ``src/`` bootstrap (#1314) — one canonical matcher
for BOTH the cold PreToolUse subprocess (here) and Lane B's shared hard-deny
registry, never a duplicated copy that drifts. Extracted whole from ``hook_router``
(the #2384 Wave-2 router split, PR4) so the dispatcher shrinks; the router
re-exports :func:`handle_block_secret_file_print` into ``_HANDLERS`` unchanged.
The deny routes through the router's shared ``_fail_open_or_deny`` chokepoint
(back-imported lazily), so the self-rescue allowlist and the ``danger_gate_fail_open``
kill-switch apply uniformly and the ``emit_pretooluse_deny`` / ``_write_pretooluse_deny``
deny writer stays in the router (the never-lockout contract).

Cold-import safe: the live PreToolUse hook is a bare ``python3`` subprocess with
no guarantee ``teatree`` is importable, so the module top imports only stdlib and
the already-extracted ``managed_repo`` sibling (the ``teatree_src_on_path``
bootstrap) — never Django / ``teatree.core``. The shared spine helper
``_fail_open_or_deny`` stays in the router and is back-imported lazily inside the
handler body.
"""

import sys

from hooks.scripts.managed_repo import teatree_src_on_path as _teatree_src_on_path

# Alias the bare and ``hooks.scripts.`` identities so the handler the router
# re-exports and a test patching a helper here operate on ONE module object.
sys.modules.setdefault("secret_file_print_guard", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.secret_file_print_guard", sys.modules[__name__])

_CREDENTIAL_PRINT_BLOCK_MSG = (
    "BLOCKED: this command would print a secret-bearing file or credential token "
    "to the transcript. Reading a secret into the transcript is irrecoverable — "
    "rotation is the only remedy. Instead, extract the value into a shell variable "
    "(`TOKEN=$(pass show …)`) and use it via env/header without printing it. "
    "Do NOT implement 'mask-then-print' — a masking regex is one edge case away "
    "from leaking. The gate's job is to keep the value off stdout entirely."
)


def _is_secret_print(command: str) -> bool:
    """Whether *command* would print a secret-bearing value to stdout.

    Delegates to the single ``teatree.hooks.secret_file_print_detect`` leaf
    (lazily imported via the sibling ``src/`` bootstrap) so the cold PreToolUse
    path and Lane B share ONE per-segment-lexed matcher. Crash-proof: any
    import/internal failure degrades to ``False`` (never wedge the tool call).
    """
    try:
        with _teatree_src_on_path():
            from teatree.hooks.secret_file_print_detect import (  # noqa: PLC0415 — deferred: cold-hook import
                is_secret_print,
            )

            return is_secret_print(command)
    except Exception:  # noqa: BLE001 — crash-proof hook: any failure degrades silently, never breaks the tool call
        return False


def handle_block_secret_file_print(data: dict) -> bool:
    """Deny a Bash command that would print a secret-bearing file or token to stdout.

    Blocks cat/head/tail of credential files, ``pass show`` without redirection,
    and echo/printf of pasted token literals. Commands that capture the value
    into a variable or redirect to a file pass through. Returns True when a
    deny was emitted (caller stops the handler chain).

    The deny routes through the router's shared ``_fail_open_or_deny`` chokepoint
    (back-imported lazily), giving the always-allowed self-rescue commands and the
    master ``danger_gate_fail_open`` kill-switch for free (the never-lockout
    contract); the ``emit_pretooluse_deny`` / ``_write_pretooluse_deny`` writer
    stays in the router.
    """
    from hooks.scripts.hook_router import _fail_open_or_deny  # noqa: PLC0415 deferred back-import

    if data.get("tool_name") != "Bash":
        return False
    command = data.get("tool_input", {}).get("command", "")
    if not command or not _is_secret_print(command):
        return False
    return _fail_open_or_deny(data, _CREDENTIAL_PRINT_BLOCK_MSG)
