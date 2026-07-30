"""The ``gh`` wrapper the CI-eval commands drive — workflow run / list / view / download.

Built on the existing :func:`teatree.backends.github.api._run_gh` seam (auth via
``GH_TOKEN`` env, never on argv, never logged), so no loop or CLI code shells a
raw ``gh``. Every method is one bounded ``gh`` subprocess and returns immediately
— the poll is stateful across ticks, never a blocking ``--watch``. The client
holds only the repo slug and an optional token; :func:`build_ci_eval_client`
resolves the token from ``GH_TOKEN`` at the point of use.
"""

import json
import os
from pathlib import Path

from teatree.backends.github.api import _run_gh
from teatree.types import RawAPIDict

#: The public repo the CI-eval heal workflow lives in.
DEFAULT_CI_EVAL_REPO = "souliane/teatree"

#: The ``workflow_dispatch``-only workflow ``ci-trigger`` dispatches and
#: ``ci-status`` resolves.
EVAL_CI_HEAL_WORKFLOW = "eval-ci-heal.yml"

#: Bound every ``gh`` call so a network stall raises (the caller degrades) rather
#: than wedging a tick indefinitely — mirrors the forge-read bound in ``api.py``.
_GH_TIMEOUT_SECONDS = 60.0


class GhCiEvalClient:
    """A thin, per-call ``gh`` client for the CI-eval heal loop's read/dispatch needs."""

    def __init__(self, repo: str, *, token: str = "") -> None:
        self.repo = repo
        self.token = token

    def trigger_workflow(self, workflow: str, *, ref: str, inputs: dict[str, str]) -> None:
        """Dispatch *workflow* against *ref* with ``-f key=value`` inputs (``gh workflow run``)."""
        args = ["gh", "workflow", "run", workflow, "--repo", self.repo, "--ref", ref]
        for key, value in inputs.items():
            args.extend(["-f", f"{key}={value}"])
        _run_gh(*args, token=self.token, timeout=_GH_TIMEOUT_SECONDS)

    def resolve_head_sha(self, ref: str) -> str:
        """The commit SHA at the tip of *ref* — the SHA a dispatch keys its run on."""
        result = _run_gh(
            "gh",
            "api",
            f"repos/{self.repo}/commits/{ref}",
            "--jq",
            ".sha",
            token=self.token,
            timeout=_GH_TIMEOUT_SECONDS,
        )
        return result.stdout.strip()

    def list_runs(self, workflow: str, *, branch: str, limit: int = 20) -> list[RawAPIDict]:
        """List recent runs of *workflow* on *branch* with the fields the FSM keys on."""
        result = _run_gh(
            "gh",
            "run",
            "list",
            "--workflow",
            workflow,
            "--repo",
            self.repo,
            "--branch",
            branch,
            "--json",
            "databaseId,headSha,status,conclusion,createdAt",
            "--limit",
            str(limit),
            token=self.token,
            timeout=_GH_TIMEOUT_SECONDS,
        )
        runs = json.loads(result.stdout or "[]")
        return runs if isinstance(runs, list) else []

    def view_run(self, run_id: int | str) -> RawAPIDict:
        """The structured verdict of one run (``status`` / ``conclusion`` / ``headSha`` / ``url``)."""
        result = _run_gh(
            "gh",
            "run",
            "view",
            str(run_id),
            "--repo",
            self.repo,
            "--json",
            "status,conclusion,headSha,url",
            token=self.token,
            timeout=_GH_TIMEOUT_SECONDS,
        )
        run = json.loads(result.stdout or "{}")
        return run if isinstance(run, dict) else {}

    def download_artifact(self, run_id: int | str, *, name: str, dest_dir: Path) -> None:
        """Download the named artifact of *run_id* into *dest_dir* (``gh run download``)."""
        _run_gh(
            "gh",
            "run",
            "download",
            str(run_id),
            "--repo",
            self.repo,
            "--name",
            name,
            "--dir",
            str(dest_dir),
            token=self.token,
            timeout=_GH_TIMEOUT_SECONDS,
        )


def build_ci_eval_client(repo: str = DEFAULT_CI_EVAL_REPO) -> GhCiEvalClient:
    """Build a client for *repo*, taking the token from ``GH_TOKEN`` at point of use."""
    return GhCiEvalClient(repo, token=os.environ.get("GH_TOKEN", ""))
