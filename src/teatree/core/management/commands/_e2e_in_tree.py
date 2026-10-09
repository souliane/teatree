"""Resolve the ``e2e in-tree`` run — the checkout's own Playwright lane.

Split out of ``e2e.py`` (mirroring the ``_e2e_discovery`` / ``_e2e_runners``
splits) so the command stays a thin typer surface over pure resolution.

``project`` runs the repo's own pytest suite and ``external`` runs a cloned
specs repo against a live stack. Neither reaches a browserless lane that lives
in the CONTRIBUTOR's checkout — a static-analysis or unit lane CI runs with no
browser, no BASE_URL and no credentials. Resolving it is three answers: which
checkout (the one the operator invoked from), which directory inside it (the
overlay's declared ``e2e_dir``), and which Playwright config (the overlay's
existing per-spec lane mapping, or an explicit ``--config``).
"""

from dataclasses import dataclass
from pathlib import Path

from teatree.core.intake.resolve import _get_user_cwd
from teatree.utils import git_run as git

_CONFIG_FLAGS = frozenset({"-c", "--config"})


class InTreeResolutionError(RuntimeError):
    """The run could not be resolved, so no Playwright process was started."""


class NotACheckoutError(InTreeResolutionError):
    def __init__(self, cwd: str) -> None:
        super().__init__(
            f"`e2e in-tree` runs the checkout it is invoked from, and {cwd} is not inside a git "
            "working tree. cd into the repo whose lane you want to run.",
        )


class MissingLaneDirError(InTreeResolutionError):
    def __init__(self, root: Path, e2e_dir: str) -> None:
        super().__init__(
            f"{root} has no '{e2e_dir}' directory. `e2e in-tree` runs the overlay's declared e2e dir "
            "(get_e2e_config()['e2e_dir']) inside the invoking checkout.",
        )


class SpecOutsideLaneDirError(InTreeResolutionError):
    def __init__(self, candidate: Path, run_dir: Path) -> None:
        super().__init__(
            f"{candidate} is outside the lane directory {run_dir} — pass a path inside it, "
            "or run from the checkout that owns the spec.",
        )


class NoLaneConfigError(InTreeResolutionError):
    def __init__(self, test_path: str, e2e_dir: str) -> None:
        super().__init__(
            f"No Playwright config resolved for {test_path or '<no spec path>'!r}. Without one Playwright "
            "loads the default config, whose global setup may log in and abort the run. Pass "
            f"--config <file> (relative to {e2e_dir}/), or a spec path the overlay maps to a lane config.",
        )


@dataclass(frozen=True)
class InTreeRun:
    run_dir: Path
    command: list[str]


def checkout_root(cwd: str) -> Path | None:
    toplevel = git.run(repo=cwd, args=["rev-parse", "--show-toplevel"])
    return Path(toplevel).resolve() if toplevel else None


def carries_config(args: list[str]) -> bool:
    return any(arg in _CONFIG_FLAGS or arg.startswith("--config=") for arg in args)


def spec_filter(test_path: str, *, e2e_dir: str, run_dir: Path) -> str:
    """Re-base *test_path* onto the e2e dir Playwright runs from.

    Playwright matches a positional filter against paths relative to its own
    cwd, so the repo-relative form a CI job and ``git status`` both use
    (``e2e/contrib/tests/x.spec.ts``) matches nothing until the e2e-dir prefix
    comes off.
    """
    cleaned = test_path.strip().removeprefix("./")
    if not cleaned:
        return ""
    candidate = Path(cleaned)
    if not candidate.is_absolute():
        return cleaned.removeprefix(f"{e2e_dir.strip('/')}/")
    try:
        return candidate.relative_to(run_dir).as_posix()
    except ValueError as exc:
        raise SpecOutsideLaneDirError(candidate, run_dir) from exc


def resolve_run(*, test_path: str, config: str, e2e_dir: str, overlay_args: list[str]) -> InTreeRun:
    cwd = _get_user_cwd()
    root = checkout_root(cwd)
    if root is None:
        raise NotACheckoutError(cwd)

    run_dir = root / e2e_dir
    if not run_dir.is_dir():
        raise MissingLaneDirError(root, e2e_dir)

    config_args = ["-c", config] if config else list(overlay_args)
    if not carries_config(config_args):
        raise NoLaneConfigError(test_path, e2e_dir)

    spec = spec_filter(test_path, e2e_dir=e2e_dir, run_dir=run_dir)
    command = ["npx", "playwright", "test", *config_args, *([spec] if spec else [])]
    return InTreeRun(run_dir=run_dir, command=command)
