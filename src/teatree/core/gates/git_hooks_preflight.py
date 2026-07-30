"""Git hook installation preflight — an install that looks working but isn't.

A checkout whose ``.git/hooks`` holds only ``*.sample`` files runs every push
with the local gate layer absent: the leak gate
(``scripts/hooks/refuse-public-push-with-leak.sh``), the banned-terms gate, and
``dev/push-gate.sh`` never fire. Nothing errors — the push just sails through
ungated, which is the same silent-broken-install class as #3523's PAT scopes.

The failure this exists to catch is not "the hooks were never installed" but
"they were installed into a DIFFERENT clone than the one work happens in": the
container's ``t3 setup`` installs into the container clone while commits and
pushes come from the host checkout. So the probe is per-CHECKOUT and
:func:`probe_checkouts` judges every checkout teatree commits from, not just the
installed one — a verdict drawn from one clone would have read green all day
while another pushed ungated.

Every worktree shares its checkout's git common dir, so verdicts collapse by
hooks dir: one probe (and one install) covers the whole family.

An explicitly-set ``core.hooksPath`` pointing somewhere other than the default
hooks dir is the operator's deliberate intent: it is reported, never stomped and
never counted as a gap.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from teatree.utils.run import CommandFailedError, run_allowed_to_fail

# Absent either of these, a commit or a push runs with no local gate layer at all.
REQUIRED_HOOK_NAMES: tuple[str, ...] = ("pre-commit", "pre-push")

# One-line "what stops firing without this" per hook.
GATES_BY_HOOK: dict[str, str] = {
    "pre-commit": "the banned-terms, main-clone and migration-scoping commit gates",
    "pre-push": "the public-repo leak gate and dev/push-gate.sh (ci-critical-parity)",
}

PREK_CONFIG_NAME = ".pre-commit-config.yaml"
INSTALL_COMMAND = "t3 setup"


@dataclass(frozen=True)
class GitHooksProbe:
    """Outcome of a git-hook installation probe on one checkout.

    ``missing`` is the required hook names absent from the resolved hooks dir
    (empty == fully installed). ``custom_hooks_path`` is a deliberate operator
    ``core.hooksPath`` override — set, it means the probe declined to judge that
    dir at all, so ``missing`` stays empty and nothing may be installed over it.
    ``indeterminate_reason`` is set when the checkout could not be inspected
    (not a repo, ``git`` unavailable) — a probe fault is never read as a gap.
    """

    checkout: Path
    hooks_dir: Path | None = None
    missing: tuple[str, ...] = ()
    custom_hooks_path: str | None = None
    indeterminate_reason: str | None = None

    @property
    def ok(self) -> bool:
        return not self.missing and self.indeterminate_reason is None

    @property
    def installable(self) -> bool:
        """True when this checkout's default hooks dir is ours to write into."""
        return self.indeterminate_reason is None and self.custom_hooks_path is None


def _git(repo: Path, *args: str) -> tuple[int, str]:
    try:
        result = run_allowed_to_fail(["git", "-C", str(repo), *args], expected_codes=None)
    except (OSError, CommandFailedError) as exc:
        return 1, str(exc)
    return result.returncode, result.stdout.strip()


def _resolve(repo: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (repo / path).resolve()


def _is_installed_hook(path: Path) -> bool:
    """A hook git will actually run: a real file with the execute bit set."""
    return path.is_file() and path.stat().st_mode & 0o111 != 0


def probe_git_hooks(repo: Path) -> GitHooksProbe:
    """Probe whether *repo*'s checkout has the required git hooks installed.

    Resolves the git COMMON dir, so probing a worktree reports the shared state
    the whole family inherits.
    """
    code, common = _git(repo, "rev-parse", "--git-common-dir")
    if code != 0 or not common:
        return GitHooksProbe(checkout=repo, indeterminate_reason=f"{repo} is not a git checkout (git rev-parse failed)")
    default_hooks_dir = (_resolve(repo, common) / "hooks").resolve()

    _code, configured = _git(repo, "config", "--get", "core.hooksPath")
    if configured and _resolve(repo, configured) != default_hooks_dir:
        return GitHooksProbe(checkout=repo, hooks_dir=_resolve(repo, configured), custom_hooks_path=configured)

    missing = tuple(name for name in REQUIRED_HOOK_NAMES if not _is_installed_hook(default_hooks_dir / name))
    return GitHooksProbe(checkout=repo, hooks_dir=default_hooks_dir, missing=missing)


def expects_hooks(repo: Path) -> bool:
    """True when *repo* declares prek hooks — the honest "this repo wants gates" predicate."""
    return (repo / PREK_CONFIG_NAME).is_file()


def probe_checkouts(checkouts: Iterable[Path]) -> list[GitHooksProbe]:
    """Probe every prek-managed checkout in *checkouts*, one verdict per git hooks dir.

    Checkouts sharing a git common dir share a verdict, so the family collapses
    to one probe. A checkout that declares no prek config is skipped — it has no
    hooks to be missing. Order follows first appearance, so the caller's
    most-authoritative checkout leads the report.
    """
    probes: list[GitHooksProbe] = []
    seen: set[Path] = set()
    for checkout in checkouts:
        if not expects_hooks(checkout):
            continue
        probe = probe_git_hooks(checkout)
        if probe.hooks_dir is not None:
            if probe.hooks_dir in seen:
                continue
            seen.add(probe.hooks_dir)
        probes.append(probe)
    return probes


def format_remediation(probe: GitHooksProbe) -> list[str]:
    """Remediation lines naming each missing hook, the gates it carries, and the fix. Pure/print-free."""
    if not probe.missing:
        return []
    lines = [f"{probe.checkout} has no git hooks installed — missing {', '.join(probe.missing)} in {probe.hooks_dir}:"]
    lines.extend(f"  {name} — carries {GATES_BY_HOOK[name]}" for name in probe.missing)
    lines.append(
        f"Every worktree sharing this git dir pushes ungated until they are installed. "
        f"Install them with: {INSTALL_COMMAND}"
    )
    return lines


__all__ = [
    "GATES_BY_HOOK",
    "INSTALL_COMMAND",
    "PREK_CONFIG_NAME",
    "REQUIRED_HOOK_NAMES",
    "GitHooksProbe",
    "expects_hooks",
    "format_remediation",
    "probe_checkouts",
    "probe_git_hooks",
]
