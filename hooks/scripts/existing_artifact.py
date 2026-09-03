"""Whether the artifact a refusal is about ALREADY exists (souliane/teatree#4151).

A gate reaches its verdict before the guarded side effect, so a refusal normally
means the artifact was never made. Gate 12 obeys that — it is a ``PreToolUse``
hook — yet the PR it guards can already be live, opened for this branch by the
pre-push ``ensure-pr`` hook on an earlier push. A bare deny then reads as
"nothing happened", and the caller retries into an ``already exists`` collision
having never tracked the PR that exists.

The answer comes from the ONE canonical tri-state probe
(``teatree.core.forge_pr_probe.find_open_pr_for_branch``) through its shell face
``t3 tool open-pr``, the same way the sibling gate shells ``t3 tool
diff-coverage`` — hand-rolling a fourth ``gh pr list`` here is exactly the drift
that probe was built to end. ``unknown`` is never read as "no PR": anything the
probe cannot answer adds no note, leaving the deny precisely as it was.

Cold-import safe: the live PreToolUse hook is a bare ``python3`` subprocess with
no guarantee ``teatree`` / Django is importable, so the module top holds stdlib
and cold-import-safe siblings only.
"""

import json
import subprocess  # noqa: S404 — hook code legitimately shells `t3` (mirrors coverage_gate).
import sys
from pathlib import Path
from typing import Final

from hooks.scripts.hook_budget import bounded_timeout_s
from hooks.scripts.t3_invocation import run_t3, t3_argv

# Alias the bare and ``hooks.scripts.`` identities to ONE module object, as every
# sibling does, so the live hook's bare import and a test's package import share globals.
sys.modules.setdefault("existing_artifact", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.existing_artifact", sys.modules[__name__])

# One forge round trip, on a deny that is already terminal — asked for, never
# assumed: it shares the hook's 30s ceiling with the measurement that preceded
# it, so ``bounded_timeout_s`` shrinks it to whatever that leaves (#4305).
_PROBE_TIMEOUT_S: Final[int] = 15


def with_open_pr_note(finding: str | None, repo_dir: Path | None, branch: str, *, elapsed: float) -> str | None:
    """Decorate a real *finding* with :func:`open_pr_note`; pass ``None`` straight through.

    Deliberately a decoration rather than a branch inside the gate's own
    ``coverage_finding_for_command``: ``None`` here is not a decline this code
    reached, it is the verdict the measurement already reached and already
    announced. Folding it in would move the #4004 decline-path count for an
    outcome that is not a new decline path at all.
    """
    if finding is None:
        return None
    note = open_pr_note(repo_dir, branch, elapsed=elapsed)
    return f"{finding}\n{note}" if note else finding


def open_pr_note(repo_dir: Path | None, branch: str, *, elapsed: float) -> str:
    """A line naming the OPEN PR that already backs *branch*, or ``""``.

    An empty *branch* lets ``t3 tool open-pr`` resolve the checked-out branch,
    which is what ``gh``/``glab`` themselves default to when a create names no
    head. Every unanswerable case — no *repo_dir*, ``t3`` off PATH, a crash, a
    timeout, an UNKNOWN tri-state — yields ``""``, so a missing credential can
    never turn into a claim about an artifact nobody saw.

    *elapsed* is what the handler has already spent of the shared hook ceiling.
    A budget it exhausts is one more unanswerable case: the note is dropped and
    the deny goes out undecorated, rather than the whole decision being lost to
    a cancelled hook (#4305).
    """
    if repo_dir is None:
        return ""
    argv = t3_argv("tool", "open-pr", "--repo", str(repo_dir))
    if argv is None:
        return ""
    timeout = bounded_timeout_s(_PROBE_TIMEOUT_S, elapsed)
    if timeout is None:
        return ""
    if branch:
        argv += ["--branch", branch]
    try:
        result = run_t3(argv, timeout=timeout, cwd=str(repo_dir))
        probe = json.loads(result.stdout or "")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, json.JSONDecodeError, ValueError):
        return ""
    if not isinstance(probe, dict) or probe.get("outcome") != "found" or not probe.get("url"):
        return ""
    return (
        f"  NOTE: a PR for this branch ALREADY EXISTS at {probe['url']} — it was created and is now "
        "FLAGGED, not refused. Do not retry the create; fix the finding on that PR."
    )
