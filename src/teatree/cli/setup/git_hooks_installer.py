"""Install the repo's prek-managed git hooks during ``t3 setup``.

Setup installed hooks into the clone it was invoked from and nowhere else, so a
containerized ``t3 setup`` left the container clone protected while the host
checkout — where commits and pushes actually happen — kept a ``.git/hooks``
holding only ``*.sample`` files. This installs into every checkout teatree
commits from, so a fresh install ends with ``pre-commit`` AND ``pre-push``
present wherever work lands.

``prek install -f`` is already idempotent by overwrite, so a re-run rewrites identical
content rather than duplicating. A hook prek did not write is parked in the quarantine
before the install and put back as ``<name>.legacy`` — unless nothing dispatches that slot,
in which case it stays parked and every line here says so until the operator moves it. A
deliberate ``core.hooksPath`` override is reported, never stomped.
"""

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from teatree.core import hook_quarantine, prek_hook
from teatree.core.gates.git_checkouts import discover_checkouts, unavailable_linked_worktree_gitdir
from teatree.core.gates.git_hooks_preflight import (
    INSTALL_COMMAND,
    REQUIRED_HOOK_NAMES,
    GitHooksProbe,
    probe_checkouts,
    probe_git_hooks,
)

logger = logging.getLogger(__name__)


class GitHooksInstaller:
    """Compose unit: ensure every checkout teatree commits from has its prek hooks."""

    def __init__(self, repo: Path, checkouts: Iterable[Path] | None = None) -> None:
        self._repo = repo
        self._checkouts = list(checkouts) if checkouts is not None else None

    def _targets(self) -> list[Path]:
        if self._checkouts is not None:
            return self._checkouts
        discovered = discover_checkouts()
        return discovered if self._repo.resolve() in discovered else [self._repo, *discovered]

    def install(self, *, echo: Callable[[str], None]) -> None:
        """Install the hooks into every checkout missing them, echoing one line each."""
        probes = probe_checkouts(self._targets())
        if not probes:
            echo("OK    No prek-managed checkout found — no git hooks to install.")
            return
        for probe in probes:
            if unavailable_linked_worktree_gitdir(probe.checkout):
                echo(
                    f"INFO  {probe.checkout}: linked worktree Git metadata unavailable in this venue — "
                    "skipped here; inspect or repair it in its owning venue."
                )
                continue
            echo(str(_qualified(probe, _ensure_installed_or_warn(probe))))


@dataclass(frozen=True, slots=True)
class _HookLine:
    """One operator line, kept as (status, text) so the status is a value and never a prefix to parse."""

    status: str
    text: str

    def __str__(self) -> str:
        return f"{self.status:<5} {self.text}"


def _qualified(probe: GitHooksProbe, line: _HookLine) -> _HookLine:
    """Re-read the quarantine from the world and demote any ``OK`` it contradicts.

    The single chokepoint every line passes through, so "no ``OK`` while a hook is parked"
    holds by construction rather than by each branch remembering to check. It re-probes
    instead of being told, which is what makes the ADVISED RE-RUN — where the hooks are
    genuinely installed and nothing in this process knows what an earlier run destroyed —
    say the same thing as the run that destroyed it.
    """
    after = probe_git_hooks(probe.checkout)
    finding = hook_quarantine.parked_finding(after.parked, after.common_dir)
    return line if finding is None else _HookLine("WARN", f"{line.text} {finding}")


def _ensure_installed_or_warn(probe: GitHooksProbe) -> _HookLine:
    """Report one checkout's failure instead of propagating it to the ones after it.

    A hooks dir holds whatever the filesystem put there and may not be writable, so a
    single poisoned checkout used to abort the loop and leave every REMAINING checkout
    ungated — and ``prepare_claude_runtime`` runs ``t3 setup`` under ``set -e``, so
    container init died on the traceback.
    """
    try:
        return _ensure_installed(probe)
    except Exception:
        logger.exception("Installing git hooks into %s failed", probe.checkout)
        return _failure_line(probe)


def _failure_line(probe: GitHooksProbe) -> _HookLine:
    """What the checkout IS after the failure — "skipping" is a claim about intent, not state.

    The install reaches destructive steps before it can fail, so the same raise covers a
    clone left exactly as found, one whose install landed and whose post-install repair did
    not, and one left with no commit or push gate at all. The re-probe alone cannot separate
    the first two — the gap this run was called to close is what does.
    """
    after = probe_git_hooks(probe.checkout)
    if after.missing:
        return _HookLine(
            "ERROR",
            f"{probe.checkout}: {', '.join(after.missing)} is NOT installed after a failed run — "
            f"every worktree of this clone commits and pushes UNGATED until `t3 setup` succeeds here.",
        )
    if probe.missing:
        return _HookLine(
            "WARN",
            f"{probe.checkout}: git hooks were re-installed but the post-install repair did not "
            f"complete — re-run `{INSTALL_COMMAND}`.",
        )
    return _HookLine("WARN", f"{probe.checkout}: git hooks left as found — setup continues.")


def _ensure_installed(probe: GitHooksProbe) -> _HookLine:
    """Install the missing hooks for one probed checkout; return the line to echo."""
    if probe.indeterminate_reason is not None:
        return _HookLine(
            "WARN", f"Could not check git hooks: {probe.indeterminate_reason} — skipping; setup continues."
        )
    if probe.custom_hooks_path is not None:
        return _HookLine(
            "OK",
            f"{probe.checkout}: core.hooksPath is set to {probe.custom_hooks_path} — "
            f"leaving the operator's hooks directory untouched.",
        )
    if probe.ok:
        return _harden_present_hooks(probe)

    result = prek_hook.install(str(probe.checkout))
    if not result.success:
        return _HookLine("WARN", f"{probe.checkout}: `prek install` failed ({result.error}) — it still pushes ungated.")
    return _installed_line(probe)


def _installed_line(probe: GitHooksProbe) -> _HookLine:
    """What the checkout IS after an install that reported success — "installed" is not the whole state.

    It says nothing about a destroyed operator gate, and it no longer has to: the parked file
    names itself in the quarantine, and :func:`_qualified` points every line at it.
    """
    after = probe_git_hooks(probe.checkout)
    if after.missing:
        return _HookLine(
            "WARN", f"{probe.checkout}: `prek install` ran but {', '.join(after.missing)} is still absent."
        )
    return _HookLine(
        "OK", f"{probe.checkout}: installed git hooks ({', '.join(REQUIRED_HOOK_NAMES)}) into {after.hooks_dir}."
    )


def _harden_present_hooks(probe: GitHooksProbe) -> _HookLine:
    """Repair a PRESENT-but-unsound hook set, and say so when the repair fired.

    A hook baked with another checkout's binary passes the probe and still refuses every
    push from the other platform, so `ok` short-circuits into a repair rather than out of
    one — and a repair reported as "already installed" is a success line over a change.
    """
    repaired = prek_hook.harden_hooks(str(probe.checkout))
    if not repaired:
        return _HookLine("OK", f"{probe.checkout}: git hooks already installed in {probe.hooks_dir}.")
    names = ", ".join(sorted(hook.name for hook in repaired))
    return _HookLine("OK", f"{probe.checkout}: re-hardened git hooks ({names}) in {probe.hooks_dir}.")
