"""Install the repo's prek-managed git hooks during ``t3 setup``.

Setup installed hooks into the clone it was invoked from and nowhere else, so a
containerized ``t3 setup`` left the container clone protected while the host
checkout — where commits and pushes actually happen — kept a ``.git/hooks``
holding only ``*.sample`` files. This installs into every checkout teatree
commits from, so a fresh install ends with ``pre-commit`` AND ``pre-push``
present wherever work lands.

``prek install -f`` is already idempotent by overwrite, so a re-run rewrites
identical content rather than duplicating. A deliberate ``core.hooksPath``
override is reported, never stomped.
"""

from collections.abc import Callable, Iterable
from pathlib import Path

from teatree.core import prek_hook
from teatree.core.gates.git_checkouts import discover_checkouts
from teatree.core.gates.git_hooks_preflight import REQUIRED_HOOK_NAMES, GitHooksProbe, probe_checkouts, probe_git_hooks


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
            echo(_ensure_installed(probe))


def _ensure_installed(probe: GitHooksProbe) -> str:
    """Install the missing hooks for one probed checkout; return the line to echo."""
    if probe.indeterminate_reason is not None:
        return f"WARN  Could not check git hooks: {probe.indeterminate_reason} — skipping; setup continues."
    if probe.custom_hooks_path is not None:
        return (
            f"OK    {probe.checkout}: core.hooksPath is set to {probe.custom_hooks_path} — "
            f"leaving the operator's hooks directory untouched."
        )
    if probe.ok:
        return f"OK    {probe.checkout}: git hooks already installed in {probe.hooks_dir}."

    result = prek_hook.install(str(probe.checkout))
    if not result.success:
        return f"WARN  {probe.checkout}: `prek install` failed ({result.error}) — it still pushes ungated."
    after = probe_git_hooks(probe.checkout)
    if after.missing:
        return f"WARN  {probe.checkout}: `prek install` ran but {', '.join(after.missing)} is still absent."
    return f"OK    {probe.checkout}: installed git hooks ({', '.join(REQUIRED_HOOK_NAMES)}) into {after.hooks_dir}."
