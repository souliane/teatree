"""Tests for the default-off eval-candidate proposer (#1933, #2346)."""

import json
import tempfile
from pathlib import Path

from django.test import TestCase

from teatree.loops.dream.engine import ConsolidationExtract, DistilledCluster, TranscriptMember, build_extract
from teatree.loops.dream.eval_proposer import ProposedEval, default_eval_proposer, propose_evals, write_eval_proposals

_CITATION = "pushed without running the gate, CI went red"


def _member(tmp: Path, name: str = "feedback_x.md") -> TranscriptMember:
    path = tmp / name
    path.write_text(f"BINDING: run the gate — {_CITATION}")
    return TranscriptMember(path=path, kind="memory")


def _extract_of(*members: TranscriptMember) -> ConsolidationExtract:
    return build_extract(list(members))


def _cited_cluster(member: TranscriptMember, *, key: str = "run-gate") -> DistilledCluster:
    return DistilledCluster(
        cluster_key=key,
        rule="Run the gate before pushing.",
        source_files=[str(member.path)],
        is_binding=False,
        verified_citation=_CITATION,
        durable_destination="feedback/run_gate.md",
    )


class DefaultEvalProposerTestCase(TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_grounded_cluster_yields_candidate_with_real_seed_citation(self) -> None:
        member = _member(self.tmp)
        proposals = default_eval_proposer([_cited_cluster(member)], _extract_of(member))
        assert len(proposals) == 1
        proposal = proposals[0]
        assert proposal.seed_citation == _CITATION
        assert proposal.scenario_name.endswith("_under_load")
        assert str(member.path) in proposal.source_files

    def test_ungrounded_cluster_yields_no_candidate(self) -> None:
        member = _member(self.tmp)
        invented = DistilledCluster(
            cluster_key="bad",
            rule="rule",
            source_files=[str(member.path)],
            is_binding=False,
            verified_citation="a mistake that never appears in the snippet text",
            durable_destination="",
        )
        assert default_eval_proposer([invented], _extract_of(member)) == []

    def test_empty_source_cluster_yields_no_candidate(self) -> None:
        member = _member(self.tmp)
        no_source = DistilledCluster(
            cluster_key="bad",
            rule="rule",
            source_files=[],
            is_binding=False,
            verified_citation=_CITATION,
            durable_destination="",
        )
        assert default_eval_proposer([no_source], _extract_of(member)) == []

    def test_propose_evals_uses_injected_proposer(self) -> None:
        sentinel = ProposedEval("x_under_load", "rule", "cite", ["p"], "")
        assert propose_evals([], _extract_of(_member(self.tmp)), proposer=lambda _c, _e: [sentinel]) == [sentinel]


class WriteEvalProposalsTestCase(TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_appends_inert_candidate_jsonl(self) -> None:
        out = self.tmp / "proposals.jsonl"
        proposal = ProposedEval("run_gate_under_load", "Run the gate.", _CITATION, ["f.md"], "feedback/x.md")
        assert write_eval_proposals([proposal], dry_run=False, out_path=out) == 1
        record = json.loads(out.read_text(encoding="utf-8").strip())
        assert record["status"] == "candidate"
        assert record["lane"] == "under_load"
        assert record["scenario_name"] == "run_gate_under_load"
        assert record["seed_citation"] == _CITATION

    def test_dry_run_writes_nothing(self) -> None:
        out = self.tmp / "proposals.jsonl"
        assert (
            write_eval_proposals([ProposedEval("x_under_load", "r", "c", ["f"], "")], dry_run=True, out_path=out) == 1
        )
        assert not out.exists()

    def test_empty_is_noop(self) -> None:
        out = self.tmp / "proposals.jsonl"
        assert write_eval_proposals([], dry_run=False, out_path=out) == 0
        assert not out.exists()
