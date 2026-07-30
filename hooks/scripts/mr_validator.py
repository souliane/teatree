"""Resolve and run the overlay MR-metadata validator subprocess.

The *execution* half of the ``validate-mr-metadata`` PreToolUse gate, split out
of ``hook_router`` for module health. It resolves the validator command and runs
it under the shared time allowance, and deliberately holds NO deny authority: the
verdict mapping and every ``emit_pretooluse_deny`` / ``_fail_open_or_deny`` call
stay in ``hook_router``, where the never-lockout contract test can see the whole
deny chain from the handler.

The return type carries the distinction that matters: a ``ValidatorTimedOut``
marker (too slow to render a verdict — CANNOT_EVALUATE, so warn and allow) is a
different outcome from a bare ``None`` (no validator exists at all — fail closed).

Cold-import safe: the live PreToolUse hook is a bare ``python3`` subprocess with
no guarantee ``teatree`` is importable, so the module top imports only stdlib and
the stdlib-only ``gate_result`` sibling.
"""

import os
import shutil
import subprocess  # noqa: S404 — stdlib subprocess for a trusted internal CLI call
import sys
from pathlib import Path

from hooks.scripts.gate_result import ValidatorTimedOut, validator_timeout_seconds

# Alias the bare and ``hooks.scripts.`` identities so the helpers the router
# imports and a test patching one here operate on ONE module object.
sys.modules.setdefault("mr_validator", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.mr_validator", sys.modules[__name__])


def mr_validate_argv() -> list[str] | None:
    """Resolve the command that validates MR metadata.

    Default (no opt-in): ``t3 tool validate-mr`` — runs the active overlay's
    ``validate_pr``, the same verdict ``t3 <overlay> pr create`` uses, so a bad
    title/description is rejected BEFORE the push every time (#119).
    ``T3_MR_VALIDATE_SCRIPT`` remains an explicit override escape hatch.
    ``None`` means no validator is resolvable — the fail-closed broken-env path.
    """
    script = os.environ.get("T3_MR_VALIDATE_SCRIPT", "")
    if script and Path(script).is_file():
        return ["python3", script]
    t3_bin = shutil.which("t3")
    if t3_bin:
        return [t3_bin, "tool", "validate-mr"]
    return None


def run_mr_validator(
    argv: list[str], title: str, description: str, target_repo: str | None = None, *, sections_optional: bool = False
) -> "subprocess.CompletedProcess[str] | ValidatorTimedOut | None":
    """Run the validator; ``ValidatorTimedOut`` if too slow, ``None`` if absent.

    ``target_repo`` (when parseable) is forwarded as ``--repo <slug>`` so the
    validator keys overlay resolution to the MR's TARGET, not the agent's cwd.
    ``sections_optional`` forwards ``--sections-optional`` for a title-only
    update whose description is untouched (#3254).
    """
    repo_args = ["--repo", target_repo] if target_repo else []
    section_args = ["--sections-optional"] if sections_optional else []
    allowance = validator_timeout_seconds()
    try:
        return subprocess.run(  # noqa: S603 — trusted internal subprocess; fixed argv, no shell
            [*argv, "--title", title, "--description", description, *repo_args, *section_args],
            capture_output=True,
            text=True,
            check=False,
            timeout=allowance,
        )
    except subprocess.TimeoutExpired:
        return ValidatorTimedOut(allowance_seconds=allowance)
    except FileNotFoundError:
        return None
