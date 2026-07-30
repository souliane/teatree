"""The full-suite green proof reads a merged eval-heal §2.4 payload (#3202).

A proof holds iff the merged run EXECUTED scenarios (``total > 0``) and recorded
ZERO reds — a red being any scenario with a non-null ``triage_class`` (behavioral,
any ``infra_*``, ``judge``, or a ``no_coverage`` skip). An empty / all-skipped
artifact is never green, so a run that eval'd nothing cannot masquerade as proof.
"""

from teatree.eval.green_proof import evaluate_green_proof


def _payload(scenarios: list[dict[str, object]], totals: dict[str, int]) -> dict[str, object]:
    return {"generated_at": "t", "model": "m", "head_sha": "sha", "totals": totals, "scenarios": scenarios}


def _pass(name: str) -> dict[str, object]:
    return {"name": name, "lane": "clean_room", "verdict": "pass", "triage_class": None}


def _red(name: str, triage_class: str, *, verdict: str = "fail") -> dict[str, object]:
    return {"name": name, "lane": "under_load", "verdict": verdict, "triage_class": triage_class}


class TestEvaluateGreenProof:
    def test_all_pass_executed_run_is_green(self) -> None:
        proof = evaluate_green_proof(
            _payload([_pass("a"), _pass("b")], {"total": 2, "passed": 2, "failed": 0, "skipped": 0})
        )
        assert proof.is_green
        assert proof.reds == ()
        assert "GREEN PROOF" in proof.summary

    def test_a_behavioral_red_is_not_green(self) -> None:
        proof = evaluate_green_proof(
            _payload([_pass("a"), _red("b", "behavioral")], {"total": 2, "passed": 1, "failed": 1, "skipped": 0})
        )
        assert not proof.is_green
        assert [r.name for r in proof.reds] == ["b"]
        assert "NOT A GREEN PROOF" in proof.summary
        assert "behavioral" in proof.summary

    def test_an_infra_red_is_not_green(self) -> None:
        # An infra_* red means the scenario never produced a clean verdict — the run
        # is not proof of green, even though a heal would retry rather than fix it.
        proof = evaluate_green_proof(
            _payload([_red("a", "infra_transport")], {"total": 1, "passed": 0, "failed": 1, "skipped": 0})
        )
        assert not proof.is_green
        assert proof.reds[0].triage_class == "infra_transport"

    def test_a_no_coverage_skip_is_not_green(self) -> None:
        proof = evaluate_green_proof(
            _payload([_red("a", "no_coverage", verdict="skip")], {"total": 1, "passed": 0, "failed": 0, "skipped": 1})
        )
        assert not proof.is_green

    def test_an_empty_run_executed_nothing_is_not_green(self) -> None:
        proof = evaluate_green_proof(_payload([], {"total": 0, "passed": 0, "failed": 0, "skipped": 0}))
        assert not proof.is_green
        assert "executed 0 scenarios" in proof.summary

    def test_a_missing_totals_or_scenarios_payload_is_not_green(self) -> None:
        assert not evaluate_green_proof({}).is_green
