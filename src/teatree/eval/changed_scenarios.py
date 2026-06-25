r"""Select the eval scenarios a PR's changed files define (the selective-PR gate).

A metered behavioral-eval run is expensive (real ``claude`` SDK trials), so a PR
should run only the scenarios it actually touched — never the whole catalog. This
module is that selector: given the PR's changed file paths (one repo-relative
POSIX path per line, exactly what ``git diff --name-only`` emits), it discovers
every spec via :func:`discover_specs` and returns the ``name`` of each spec whose
``source_path`` — normalized to repo-relative — equals one of the changed paths.
One YAML file may define several scenarios sharing a ``source_path``, so editing
that file selects all of them.

``source_path`` is absolute (``discover_specs`` globs an absolute catalog dir),
while the diff gives repo-relative paths; both are normalized to repo-relative
before comparing so the match is representation-independent.

The selective-PR lane (``eval-pr.yml``) runs the selected scenarios SEQUENTIALLY
in ONE job at a single trial, so the selection is capped at
:data:`MAX_SELECTIVE_PR_SCENARIOS`: a PR that mechanically touches every scenario
file (a ``model:``→``tier:`` backfill, a mass rename) would otherwise select the
whole ~210-scenario catalog and blow past the 80-min step cap — the cancellation
that reddened PR #2726's eval job. Full coverage of a corpus-wide change is the
WEEKLY sharded lane's job (``eval-weekly-reusable.yml`` fans the catalog into
budget-safe shards via :mod:`teatree.eval.lane_shard`); the PR lane stays "bounded
by what changed" (BLUEPRINT §"Selective-PR eval") by capping at this ceiling.

This is the shared core behind both ``t3 eval changed-scenarios`` (the reusable
overlay-facing CLI) and ``scripts/eval/scenarios_for_changed.py`` (the thin
script the host workflow shells out to). Both delegate here; the logic lives once.
"""

from collections.abc import Iterable
from pathlib import Path

from teatree.eval.discovery import SCENARIOS_DIR, discover_specs
from teatree.eval.lane_shard import MAX_SCENARIOS_PER_SHARD
from teatree.eval.models import EvalSpec

REPO_ROOT = SCENARIOS_DIR.parents[1]

#: The most scenarios the single-job, single-trial selective-PR lane will run.
#: A clean_room scenario runs in seconds-to-a-minute, so this matches the weekly
#: lane's budget-safe per-shard ceiling (:data:`MAX_SCENARIOS_PER_SHARD`): a
#: sequential single-trial run of this many finishes well inside the 80-min step
#: cap. A diff that selects MORE than this (a corpus-wide mechanical edit) is
#: truncated to a deterministic sorted-name prefix — the weekly sharded lane gives
#: the full-catalog coverage the bounded PR lane intentionally does not.
MAX_SELECTIVE_PR_SCENARIOS = MAX_SCENARIOS_PER_SHARD


def _relative_to_root(path: Path, repo_root: Path) -> str:
    candidate = path if path.is_absolute() else repo_root / path
    try:
        return candidate.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return candidate.as_posix()


def names_for_changed(changed: Iterable[str], specs: Iterable[EvalSpec], repo_root: Path) -> list[str]:
    wanted = {_relative_to_root(Path(line.strip()), repo_root) for line in changed if line.strip()}
    matched = {spec.name for spec in specs if _relative_to_root(spec.source_path, repo_root) in wanted}
    # Cap to keep the single-job PR lane inside its step budget; the sorted prefix
    # is deterministic so the same diff always selects the same bounded subset.
    return sorted(matched)[:MAX_SELECTIVE_PR_SCENARIOS]


def select_changed_scenario_names(changed: Iterable[str]) -> list[str]:
    return names_for_changed(changed, discover_specs(), REPO_ROOT)
