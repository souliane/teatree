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
the stdlib-only ``gate_result`` / ``t3_invocation`` siblings.
"""

import json
import os
import subprocess  # noqa: S404 — the CompletedProcess return type; the spawn itself is the seam's
import sys
from pathlib import Path

from hooks.scripts.gate_result import GateSkipped, ValidatorTimedOut, validator_timeout_seconds
from hooks.scripts.t3_invocation import run_t3, t3_argv

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
    return t3_argv("tool", "validate-mr")


_EXEC_FAILED_REASON = (
    "the overlay validator could not be EXECUTED at all ({exc}), so the title and description were never checked"
)


def run_mr_validator(
    argv: list[str], title: str, description: str, target_repo: str | None = None, *, sections_optional: bool = False
) -> "subprocess.CompletedProcess[str] | ValidatorTimedOut | GateSkipped | None":
    """Run the validator; a marker when it rendered no verdict, ``None`` if absent.

    The title/description pair goes on STDIN as one JSON object; only the short, bounded
    flags ride argv. GitLab accepts a 1 MiB description and a description-only edit
    back-fills ``title=description``, so on argv the body rode the exec TWICE and
    breached ``ARG_MAX`` (1048576 here) at roughly half the size the forge itself allows.
    ``target_repo`` (when parseable) is forwarded as ``--repo <slug>`` so the validator
    keys overlay resolution to the MR's TARGET, not the agent's cwd. ``sections_optional``
    forwards ``--sections-optional`` for a title-only update whose description is
    untouched (#3254).

    A spawn that fails outright returns :class:`GateSkipped` — CANNOT_EVALUATE, which
    the caller announces and allows. Letting the ``OSError`` propagate instead made the
    gate fail OPEN in SILENCE: the router wraps every handler in ``except Exception:
    continue``, so an over-``ARG_MAX`` body exited 0 with empty stdout, byte-identical
    to a clean pass on text no validator had read.
    """
    repo_args = ["--repo", target_repo] if target_repo else []
    section_args = ["--sections-optional"] if sections_optional else []
    allowance = validator_timeout_seconds()
    try:
        return run_t3(
            [*argv, *repo_args, *section_args],
            timeout=allowance,
            stdin_text=json.dumps({"title": title, "description": description}),
        )
    except subprocess.TimeoutExpired:
        return ValidatorTimedOut(allowance_seconds=allowance)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return GateSkipped(reason=_EXEC_FAILED_REASON.format(exc=exc))
