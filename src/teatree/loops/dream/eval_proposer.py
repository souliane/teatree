"""Inert eval-candidate derivation for the dream pass (#1933, #2346).

Phase-3b of the dream pass (default OFF): turn the engine's GROUNDED drift
clusters into eval CANDIDATES — descriptors a core-maker / human ratifies into a
real anti-vacuous ``under_load`` scenario under ``evals/scenarios/`` (pollution
preamble + discriminating matchers + ``_pass``/``_fail`` fixtures + the teeth
proof). This module deliberately writes no scenario file, fixture, or test; it appends candidate
descriptors to a JSONL review queue, the same high-blast-radius boundary that
defers the engine's phase-4/5/6 file rewrites. The LLM-generated,
self-anti-vacuous derivation is the deferred follow-up the design issue specifies.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypedDict

from teatree.loops.dream.engine import (
    ConsolidationExtract,
    DistilledCluster,
    cluster_is_grounded,
    default_projects_dir,
    normalize_ws,
)


@dataclass(frozen=True, slots=True)
class ProposedEval:
    """An inert eval CANDIDATE derived from a grounded drift cluster.

    A proposal is a descriptor, never a written eval: it names the drift, carries
    the real cited mistake that seeds a ``_fail`` fixture, and suggests a home.
    """

    scenario_name: str
    drift_rule: str
    seed_citation: str
    source_files: list[str]
    suggested_destination: str


class EvalProposer(Protocol):
    """The seam: grounded clusters + extract → inert eval candidates."""

    def __call__(self, clusters: Sequence[DistilledCluster], extract: ConsolidationExtract) -> list[ProposedEval]: ...


@dataclass(frozen=True, slots=True)
class EvalProposalRequest:
    """Activates the default-off eval-candidate phase — presence IS the toggle.

    Passing ``eval_proposals=None`` to ``run_consolidation`` keeps the phase OFF
    and the pass byte-identical; passing a request turns it on. ``proposer``
    overrides the default grounded-cluster proposer (tests inject a fake) and
    ``out_path`` overrides the review-queue location.
    """

    proposer: EvalProposer | None = None
    out_path: Path | None = None


class ProposalRecord(TypedDict):
    """One JSONL row in the review queue — an inert candidate, never an eval."""

    scenario_name: str
    drift_rule: str
    seed_citation: str
    source_files: list[str]
    suggested_destination: str
    lane: str
    status: str


def _eval_scenario_name(cluster_key: str) -> str:
    slug = "".join(ch if ch.isalnum() else "_" for ch in cluster_key.strip().lower())
    slug = "_".join(part for part in slug.split("_") if part)
    base = slug or "drift"
    return base if base.endswith("under_load") else f"{base}_under_load"


def default_eval_proposer(clusters: Sequence[DistilledCluster], extract: ConsolidationExtract) -> list[ProposedEval]:
    """Map each GROUNDED cluster to one inert eval candidate (no LLM, no file write).

    Reuses :func:`teatree.loops.dream.engine.cluster_is_grounded` — the same guard
    ``write_clusters`` applies — so a proposal is emitted ONLY for a cluster citing
    a real mistake present in the extract. The cited mistake becomes
    ``seed_citation``: the seed of the eventual ``_fail`` fixture. A cluster with
    no grounded citation yields no proposal, so the proposer can never invent an
    eval for a drift it cannot point at.
    """
    snippet_texts = {str(snippet.path): normalize_ws(snippet.text) for snippet in extract.snippets}
    proposals: list[ProposedEval] = []
    for cluster in clusters:
        if not cluster_is_grounded(cluster, snippet_texts):
            continue
        proposals.append(
            ProposedEval(
                scenario_name=_eval_scenario_name(cluster.cluster_key),
                drift_rule=cluster.rule,
                seed_citation=cluster.verified_citation,
                source_files=[str(path) for path in cluster.source_files],
                suggested_destination=cluster.durable_destination,
            )
        )
    return proposals


def propose_evals(
    clusters: Sequence[DistilledCluster],
    extract: ConsolidationExtract,
    *,
    proposer: EvalProposer | None = None,
) -> list[ProposedEval]:
    """Derive inert eval candidates from the distilled clusters via the seam."""
    propose = proposer or default_eval_proposer
    return propose(clusters, extract)


def _default_proposals_path() -> Path:
    return default_projects_dir() / "dream-eval-proposals.jsonl"


def write_eval_proposals(proposals: Sequence[ProposedEval], *, dry_run: bool, out_path: Path | None = None) -> int:
    """Append inert eval candidates to the review queue; never write an eval/fixture.

    The output is a JSONL CANDIDATE queue a core-maker / human ratifies into a real
    anti-vacuous ``under_load`` scenario under ``evals/scenarios/`` — this function
    deliberately writes no scenario file, no fixture, and no test. Returns the count;
    under *dry_run* the count is computed but nothing is written.
    """
    if not proposals:
        return 0
    if dry_run:
        return len(proposals)
    path = out_path or _default_proposals_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(_proposal_record(proposal), sort_keys=True) for proposal in proposals]
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return len(proposals)


def _proposal_record(proposal: ProposedEval) -> ProposalRecord:
    return ProposalRecord(
        scenario_name=proposal.scenario_name,
        drift_rule=proposal.drift_rule,
        seed_citation=proposal.seed_citation,
        source_files=proposal.source_files,
        suggested_destination=proposal.suggested_destination,
        lane="under_load",
        status="candidate",
    )


__all__ = [
    "EvalProposalRequest",
    "EvalProposer",
    "ProposalRecord",
    "ProposedEval",
    "default_eval_proposer",
    "propose_evals",
    "write_eval_proposals",
]
