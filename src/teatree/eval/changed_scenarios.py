r"""Select the eval scenarios a PR's changed files define (the selective-PR gate).

A metered behavioral-eval run is expensive (real ``claude`` SDK trials), so a PR
should run only the scenarios it actually touched — never the whole catalog. This
module is that selector: given the PR's changed file paths (one repo-relative
POSIX path per line, exactly what ``git diff --name-only`` emits), it discovers
every spec via :func:`discover_specs` and returns the ``name`` of each spec the
PR touched. A spec is touched two ways:

*   its ``source_path`` — the scenario YAML — is one of the changed paths. One
    YAML may define several scenarios, so editing it selects all of them.
*   its ``agent_path`` — the agent-facing skill file whose prose IS the graded
    system prompt — is one of the changed paths (#3944). Keying on the YAML alone
    meant editing the exact section a scenario grades selected ZERO scenarios, and
    the lane reported PASS having proven nothing about the subject matter that
    moved. When the caller supplies ``changed_sections`` (from
    :mod:`teatree.eval.changed_sections`), a spec declaring ``agent_sections`` is
    selected only if one of ITS sections changed, so editing an ungraded section of
    a shared skill file selects nothing extra; a spec with no ``agent_sections``
    grades the whole file and any edit selects it.

``source_path`` is absolute (``discover_specs`` globs an absolute catalog dir),
while the diff gives repo-relative paths; both are normalized to repo-relative
before comparing so the match is representation-independent. ``agent_path``
normalizes through the same function.

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

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class ScenarioSelection:
    """A capped selective-PR selection plus the truncation the cap applied (#2737).

    ``names`` is the bounded subset the lane runs; ``total_matched`` is how many the
    diff selected BEFORE the cap. When ``total_matched`` exceeds the cap the extra
    scenarios are deferred to the weekly sharded lane — :meth:`truncation_note` renders
    the one-line signal so a corpus-wide PR's truncated coverage is visible in the CI
    log instead of hidden.
    """

    names: list[str]
    total_matched: int
    cap: int

    @property
    def truncated(self) -> bool:
        return self.total_matched > self.cap

    @property
    def deferred(self) -> int:
        return max(self.total_matched - len(self.names), 0)

    def truncation_note(self) -> str | None:
        if not self.truncated:
            return None
        return (
            f"selected {self.total_matched} changed scenarios, capped to {self.cap} for the "
            f"selective-PR lane; deferred {self.deferred} to the weekly sharded lane"
        )


#: Selection bands, sorted ahead of the name so the cap can never truncate away the
#: TIGHTEST evidence in favour of a broader match (#3944). ``PRECISE`` is "this diff
#: touched exactly what the scenario is built from" — its own YAML, or a section it
#: names. ``BROAD`` is "the scenario grades the whole file, or the granularity is
#: unknown", which any edit to the file satisfies.
_PRECISE = 0
_BROAD = 1


def _relative_to_root(path: Path, repo_root: Path) -> str:
    candidate = path if path.is_absolute() else repo_root / path
    try:
        return candidate.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return candidate.as_posix()


def _band(
    spec: EvalSpec,
    wanted: set[str],
    changed_sections: Mapping[str, frozenset[str]],
    repo_root: Path,
) -> int | None:
    """The selection band for *spec*, or ``None`` when this diff does not select it."""
    if _relative_to_root(spec.source_path, repo_root) in wanted:
        return _PRECISE
    if not spec.agent_path:
        return None
    agent_path = _relative_to_root(Path(spec.agent_path), repo_root)
    if agent_path not in wanted:
        return None
    if not spec.agent_sections:
        # No narrowing declared, so the WHOLE file is the graded system prompt.
        return _BROAD
    touched = changed_sections.get(agent_path)
    if touched is None:
        return _BROAD
    return _PRECISE if touched.intersection(spec.agent_sections) else None


def selection_for_changed(
    changed: Iterable[str],
    specs: Iterable[EvalSpec],
    repo_root: Path,
    *,
    changed_sections: Mapping[str, frozenset[str]] | None = None,
) -> ScenarioSelection:
    wanted = {_relative_to_root(Path(line.strip()), repo_root) for line in changed if line.strip()}
    sections = changed_sections or {}
    banded = [(band, spec.name) for spec in specs if (band := _band(spec, wanted, sections, repo_root)) is not None]
    # Cap to keep the single-job PR lane inside its step budget; band-then-name is
    # deterministic, so the same diff always selects the same bounded subset.
    return ScenarioSelection(
        names=[name for _, name in sorted(set(banded))][:MAX_SELECTIVE_PR_SCENARIOS],
        total_matched=len(banded),
        cap=MAX_SELECTIVE_PR_SCENARIOS,
    )


def names_for_changed(
    changed: Iterable[str],
    specs: Iterable[EvalSpec],
    repo_root: Path,
    *,
    changed_sections: Mapping[str, frozenset[str]] | None = None,
) -> list[str]:
    return selection_for_changed(changed, specs, repo_root, changed_sections=changed_sections).names


def specs_under(specs: Iterable[EvalSpec], scenarios_dir: Path) -> list[EvalSpec]:
    """The specs whose ``source_path`` lives under ``scenarios_dir`` — the per-consumer catalog filter.

    :func:`discover_specs` returns the whole union catalog (core plus every installed overlay's
    scenarios). A consuming repo owns only the scenarios under its OWN directory, so it passes that
    directory here: without the filter a PR touching a core scenario file would drag scenarios that
    are not the consumer's into the consumer's PR lane. Both sides are ``resolve()``-normalized so the
    match is representation-independent (the same discipline :func:`_relative_to_root` uses).
    """
    root = scenarios_dir.resolve()
    return [spec for spec in specs if spec.source_path.resolve().is_relative_to(root)]


def select_changed_scenarios(
    changed: Iterable[str],
    *,
    repo_root: Path = REPO_ROOT,
    specs: Iterable[EvalSpec] | None = None,
    changed_sections: Mapping[str, frozenset[str]] | None = None,
) -> ScenarioSelection:
    """The full selection (names + truncation) the selective-PR entry points surface.

    ``repo_root`` is what the diff paths are relative to; it defaults to teatree's own repo root, so
    the host lane's ``t3 eval changed-scenarios`` (and ``scripts/eval/scenarios_for_changed.py``) stay
    byte-for-byte unchanged. ``specs`` defaults to the whole union catalog (:func:`discover_specs`); a
    consuming overlay passes its own scenarios-dir-filtered subset (see :func:`specs_under`).
    ``changed_sections`` is the optional prose-granularity refinement from
    :func:`teatree.eval.changed_sections.changed_sections_by_path`; omitting it keeps the fail-safe
    whole-file reading of every changed ``agent_path``.
    """
    resolved_specs = discover_specs() if specs is None else specs
    return selection_for_changed(changed, resolved_specs, repo_root, changed_sections=changed_sections)
