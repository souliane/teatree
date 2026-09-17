"""t3 tool verify-gates -- the one CI-parity local gate command.

Registers onto the shared tool_app (side-effect import from cli/__init__,
mirroring comment_density_tools / test_shape_tools).

A plain ``prek run --all-files`` only fires the commit/manual-stage hooks:
``.pre-commit-config.yaml`` sets ``default_stages: [commit, manual]``. The
push-stage gates (refuse-public-push-with-leak, doc-update-gate,
comment-density, ensure-pr) carry ``stages: [push]`` and are STRUCTURALLY
skipped -- yet CI re-runs them on the PR-vs-base diff. So a builder reporting
"local prek is green" can be honest about the commit-stage hooks while blind
to the exact push-stage gate CI fails on.

This command runs BOTH stages -- ``prek run --all-files`` and
``prek run --all-files --hook-stage pre-push`` -- against the working tree and
returns the combined exit code as the single green-proof, making "local green"
== "CI green" by construction. Skills and the headless builder dispatch prompt
point at this one command instead of the bare ``prek run --all-files``.

Note the stage name: prek's ``--hook-stage`` accepts the canonical
``pre-push`` value (the config's ``stages: [push]`` is the legacy alias prek
maps onto ``pre-push``). The literal ``--hook-stage push`` is rejected by prek.

It also DISCLOSES the tree it graded (#4720). The command takes no positional
target, so run from a main clone it measures the default branch rather than the
branch under review and still exits 0 -- a reviewer handing back that exit code
reports a green for a tree nobody asked about. So every run names the checkout,
sha and branch it measured, an explicit ``--expect-sha`` is a hard target check,
a clean main clone on its default branch is refused, and a green run names the
CI jobs no local hook covers so exit 0 cannot be read as "CI will be green".
"""

import shutil
from dataclasses import dataclass
from pathlib import Path

import typer

from teatree.utils.git_branch import current_branch, head_sha
from teatree.utils.git_remote_ops import config_value
from teatree.utils.git_run import run, run_strict
from teatree.utils.run import CommandFailedError, run_streamed

# The same key ``refuse-main-clone-commit.sh`` reads, so one declaration serves both.
_TARGET_BRANCH_CONFIG_KEY = "teatree.targetBranch"

# prek's ``--hook-stage`` flag expects the canonical stage name. The config's
# ``stages: [push]`` alias resolves to this; passing ``push`` verbatim errors.
_PUSH_STAGE = "pre-push"

_EXPECT_SHA_ENV = "T3_VERIFY_GATES_EXPECT_SHA"

# Distinct from 1 (a gate failed) so a caller can tell "this tree is red" from
# "nothing was measured" -- the two produce opposite next actions.
_WRONG_TREE_EXIT = 2

#: CI jobs that NO commit- or push-stage hook runs, so a green here says nothing
#: about them. Pinned against ``.github/workflows/ci.yml`` by the test suite.
UNCOVERED_CI_JOBS = (
    "test-shard",
    "test",
    "test-shuffle",
    "mutation-diff",
    "jscpd-scan",
    "selection-audit",
    "refresh-durations",
)


@dataclass(frozen=True, slots=True)
class MeasuredTree:
    """The checkout a verify-gates run actually graded."""

    toplevel: Path
    head_sha: str
    branch: str
    is_primary_clone: bool
    default_branch: str
    target_branch: str
    dirty: bool

    @property
    def on_integration_branch(self) -> bool:
        """Is HEAD the branch this clone exists to HOLD rather than develop on?

        A fork whose work lands on a long-lived integration branch declares it in
        git config under ``_TARGET_BRANCH_CONFIG_KEY``, and its clone sits there the
        way ours sits on the default — same structural non-target, same refusal.
        """
        return bool(self.branch) and self.branch in {self.default_branch, self.target_branch}

    @property
    def venue(self) -> str:
        return "main clone" if self.is_primary_clone else "worktree"

    def describe(self) -> str:
        dirty = ", dirty" if self.dirty else ""
        return f"{self.toplevel} @ {self.head_sha} ({self.branch}{dirty}, {self.venue})"


def read_measured_tree(repo: str = ".") -> MeasuredTree | None:
    """The checkout at *repo*, or ``None`` when it is not a git tree."""
    try:
        toplevel = Path(run_strict(repo=repo, args=["rev-parse", "--show-toplevel"]))
        sha = head_sha(repo=repo)
        git_dir = Path(run_strict(repo=repo, args=["rev-parse", "--absolute-git-dir"]))
    except (CommandFailedError, OSError):
        return None
    return MeasuredTree(
        toplevel=toplevel,
        head_sha=sha,
        branch=current_branch(repo=repo),
        # A linked worktree's ``.git`` is a FILE pointing into the clone's admin
        # dir, so only the primary checkout's git dir IS ``<toplevel>/.git``.
        is_primary_clone=git_dir == toplevel / ".git",
        default_branch=_default_branch(repo),
        target_branch=config_value(repo, _TARGET_BRANCH_CONFIG_KEY).strip(),
        # Untracked-insensitive: ``prek run --all-files`` grades TRACKED files, so a
        # scratch file nobody added changes nothing about what was measured.
        dirty=bool(run(repo=repo, args=["status", "--porcelain", "--untracked-files=no"])),
    )


def _default_branch(repo: str) -> str:
    """The remote's default branch, or ``main`` when ``origin/HEAD`` is unset."""
    ref = run(repo=repo, args=["symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    return ref.removeprefix("origin/") or "main"


def wrong_tree_refusal(tree: MeasuredTree, *, expected: str, allow_main_clone: bool) -> str:
    """Why *tree* must not be graded, or ``""`` when it may be."""
    if expected:
        # Naming the sha IS saying which tree you meant, so the main-clone
        # refusal below has nothing left to add.
        # git emits lowercase; a sha pasted from a forge UI may not be.
        if tree.head_sha.lower().startswith(expected.lower()):
            return ""
        return (
            f"verify-gates: this tree is at {tree.head_sha}, NOT the target {expected} — nothing "
            "measured. cd into the ticket worktree, or "
            "`t3 review checkout <pr-url> --sha <head>`."
        )
    if allow_main_clone or tree.dirty or not (tree.is_primary_clone and tree.on_integration_branch):
        return ""
    return (
        f"verify-gates: refusing to grade the main clone on {tree.branch} @ {tree.head_sha} — that "
        "measures the default branch, not a PR, and its exit 0 is not a green-proof for anything "
        "under review. Grade the ticket worktree, pass --expect-sha <head>, or --allow-main-clone."
    )


def _prek_available() -> bool:
    return shutil.which("prek") is not None


def verify_gates(
    expect_sha: str = typer.Option(
        "",
        "--expect-sha",
        envvar=_EXPECT_SHA_ENV,
        help="Full or abbreviated SHA this tree must be at; any other tree is refused.",
    ),
    *,
    allow_main_clone: bool = typer.Option(
        False,
        "--allow-main-clone",
        help="Grade a clean main clone on its default branch (refused by default).",
    ),
) -> None:
    """Run the FULL CI-equivalent local gate set (commit AND push stages).

    Runs ``prek run --all-files`` then ``prek run --all-files --hook-stage
    pre-push`` and exits non-zero if EITHER stage fails. The push-stage run is
    what catches the gates CI fails on but a bare ``prek run --all-files``
    cannot see (comment-density, doc-update, ensure-pr, the public-repo leak
    gate). The full test suite is NOT a push gate -- push -> CI runs it.

    Report the measured SHA it prints TOGETHER WITH its exit code as the
    green-proof — an exit code alone does not say which tree earned it. Exits 2
    without grading anything when the tree is not a git checkout, is not the
    ``--expect-sha`` target, or is a clean main clone on its default branch.
    """
    if not _prek_available():
        typer.echo(
            "verify-gates: prek not found on PATH. Install prek (the pre-commit "
            "runner) so the local gate set matches CI.",
            err=True,
        )
        raise typer.Exit(code=1)

    tree = read_measured_tree()
    if tree is None:
        typer.echo(f"verify-gates: {Path.cwd()} is not a git tree — nothing measured.", err=True)
        raise typer.Exit(code=_WRONG_TREE_EXIT)

    refusal = wrong_tree_refusal(tree, expected=expect_sha.strip(), allow_main_clone=allow_main_clone)
    if refusal:
        typer.echo(refusal, err=True)
        raise typer.Exit(code=_WRONG_TREE_EXIT)

    typer.echo(f"verify-gates: measuring {tree.describe()}", err=True)

    stages = (
        ("commit + manual", ["prek", "run", "--all-files"]),
        ("pre-push (CI-parity gates)", ["prek", "run", "--all-files", "--hook-stage", _PUSH_STAGE]),
    )
    failed: list[str] = []
    for label, cmd in stages:
        typer.echo(f"== verify-gates: {label} ==", err=True)
        # ``check=False`` inherits stdio (live per-hook output) and lets us
        # collect every failing stage in one pass instead of stopping at the first.
        if run_streamed(cmd, check=False) != 0:
            failed.append(label)

    if failed:
        typer.echo(
            f"verify-gates: FAILED stage(s): {', '.join(failed)} — measured {tree.head_sha}.",
            err=True,
        )
        raise typer.Exit(code=1)
    typer.echo(
        f"verify-gates: all gate stages green (commit + push) — measured {tree.head_sha}.",
        err=True,
    )
    typer.echo(
        f"verify-gates: NOT covered by any local hook: {', '.join(UNCOVERED_CI_JOBS)} — CI at the "
        "pushed SHA is the authority (`gh pr checks <n>`).",
        err=True,
    )


def register(app: typer.Typer) -> None:
    """Register this module's ``t3 tool`` command(s) onto *app* (called from ``cli/__init__``)."""
    app.command("verify-gates")(verify_gates)
