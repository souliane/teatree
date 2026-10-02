"""The full-suite green proof reads a merged eval-heal §2.4 payload (#3202).

A proof holds iff the merged run COVERED the catalog it claims to prove (every
expected scenario counted, one row per counted scenario) and recorded ZERO reds
— a red being any scenario with a non-null ``triage_class`` (behavioral, any
``infra_*``, ``judge``, or a ``no_coverage`` skip). An empty, all-skipped, or
missing-shard artifact is never green, so a run that eval'd part of the suite
cannot masquerade as proof of all of it.
"""

from teatree.eval.green_proof import UNCLASSIFIED, evaluate_green_proof


def _payload(scenarios: list[dict[str, object]], totals: dict[str, int]) -> dict[str, object]:
    return {"generated_at": "t", "model": "m", "head_sha": "sha", "totals": totals, "scenarios": scenarios}


def _totals(scenarios: list[dict[str, object]]) -> dict[str, int]:
    return {
        "total": len(scenarios),
        "passed": sum(1 for s in scenarios if s.get("verdict") == "pass"),
        "failed": sum(1 for s in scenarios if s.get("verdict") == "fail"),
        "skipped": sum(1 for s in scenarios if s.get("verdict") == "skip"),
    }


def _pass(name: str) -> dict[str, object]:
    return {
        "name": name,
        "version": "v",
        "lane": "clean_room",
        "verdict": "pass",
        "outcome": "PASS",
        "triage_class": None,
    }


def _red(name: str, triage_class: str, *, verdict: str = "fail") -> dict[str, object]:
    outcome = (
        "UNVERIFIED" if verdict == "skip" else "INFRA_BLOCKED" if triage_class.startswith("infra_") else "BEHAVIOR_FAIL"
    )
    return {
        "name": name,
        "version": "v",
        "lane": "under_load",
        "verdict": verdict,
        "outcome": outcome,
        "triage_class": triage_class,
    }


def _expected(scenarios: list[dict[str, object]]) -> dict[str, str]:
    return {str(row["name"]): str(row["version"]) for row in scenarios}


class TestEvaluateGreenProof:
    def test_a_run_covering_the_catalog_with_no_reds_is_green(self) -> None:
        scenarios = [_pass("a"), _pass("b")]
        proof = evaluate_green_proof(
            _payload(scenarios, _totals(scenarios)), expected=_expected(scenarios), expected_sha="sha"
        )
        assert proof.is_green
        assert proof.reds == ()
        assert "GREEN PROOF" in proof.summary

    def test_retry_recovery_is_flaky_and_never_counted_as_clean_pass(self) -> None:
        scenarios = [_pass("a"), _red("b", "behavioral")]
        scenarios[1]["outcome"] = "FLAKY"
        proof = evaluate_green_proof(
            _payload(scenarios, _totals(scenarios)), expected=_expected(scenarios), expected_sha="sha"
        )
        assert proof.is_green
        assert "1 PASS, 1 FLAKY" in proof.summary

    def test_a_behavioral_red_is_not_green(self) -> None:
        scenarios = [_pass("a"), _red("b", "behavioral")]
        proof = evaluate_green_proof(
            _payload(scenarios, _totals(scenarios)), expected=_expected(scenarios), expected_sha="sha"
        )
        assert not proof.is_green
        assert [r.name for r in proof.reds] == ["b"]
        assert "NOT A GREEN PROOF" in proof.summary
        assert "behavioral" in proof.summary

    def test_an_infra_red_is_not_green(self) -> None:
        # An infra_* red means the scenario never produced a clean verdict — the run
        # is not proof of green, even though a heal would retry rather than fix it.
        scenarios = [_red("a", "infra_transport")]
        proof = evaluate_green_proof(
            _payload(scenarios, _totals(scenarios)), expected=_expected(scenarios), expected_sha="sha"
        )
        assert not proof.is_green
        assert proof.reds[0].triage_class == "infra_transport"

    def test_a_no_coverage_skip_is_not_green(self) -> None:
        scenarios = [_red("a", "no_coverage", verdict="skip")]
        proof = evaluate_green_proof(
            _payload(scenarios, _totals(scenarios)), expected=_expected(scenarios), expected_sha="sha"
        )
        assert not proof.is_green

    def test_missing_grader_on_advisory_surface_is_unverified_and_blocks(self) -> None:
        scenarios = [_red("a", "behavioral")]
        scenarios[0]["outcome"] = "UNVERIFIED"
        scenarios[0]["advisory"] = True
        proof = evaluate_green_proof(_payload(scenarios, _totals(scenarios)), expected={"a": "v"}, expected_sha="sha")
        assert not proof.is_green

    def test_an_empty_run_executed_nothing_is_not_green(self) -> None:
        proof = evaluate_green_proof(_payload([], _totals([])), expected={"a": "v"}, expected_sha="sha")
        assert not proof.is_green
        assert "covered 0 scenario(s)" in proof.summary

    def test_a_missing_totals_or_scenarios_payload_is_not_green(self) -> None:
        assert not evaluate_green_proof({}, expected={"a": "v"}, expected_sha="sha").is_green


class TestCatalogCoverage:
    """The lost-shard hole: an internally-consistent payload that covers part of the suite."""

    def test_a_surviving_shard_alone_is_not_the_full_suite_proof(self) -> None:
        # Seven of eight shards died at the CLI presence-gate and uploaded nothing.
        # The one that ran is internally consistent and all-green — and proves
        # nothing about the 202 scenarios it never carried.
        scenarios = [_pass(f"s{index}") for index in range(29)]
        proof = evaluate_green_proof(
            _payload(scenarios, _totals(scenarios)), expected={f"s{i}": "v" for i in range(231)}, expected_sha="sha"
        )
        assert not proof.is_green
        assert proof.reds == ()
        assert "29 scenario(s)" in proof.summary
        assert "231" in proof.summary

    def test_a_payload_whose_rows_are_fewer_than_its_totals_is_not_green(self) -> None:
        # The producer writes one row per counted scenario, so a totals block that
        # outruns the row list is a truncated payload, not a green run.
        scenarios = [_pass("a"), _pass("b")]
        proof = evaluate_green_proof(
            _payload(scenarios, {"total": 231, "passed": 231, "failed": 0, "skipped": 0}),
            expected=_expected(scenarios),
            expected_sha="sha",
        )
        assert not proof.is_green

    def test_totals_must_agree_with_each_row_verdict(self) -> None:
        scenarios = [_pass("a")]
        proof = evaluate_green_proof(
            _payload(scenarios, {"total": 1, "passed": 0, "failed": 1, "skipped": 0}),
            expected={"a": "v"},
            expected_sha="sha",
        )
        assert not proof.is_green

    def test_an_empty_sha_cannot_prove_a_checkout(self) -> None:
        payload = _payload([_pass("a")], {"total": 1, "passed": 1, "failed": 0, "skipped": 0})
        payload["head_sha"] = ""
        proof = evaluate_green_proof(payload, expected={"a": "v"}, expected_sha="")
        assert not proof.is_green

    def test_a_run_wider_than_the_expected_catalog_does_not_prove_it(self) -> None:
        scenarios = [_pass(f"s{index}") for index in range(5)]
        proof = evaluate_green_proof(
            _payload(scenarios, _totals(scenarios)), expected={f"s{i}": "v" for i in range(3)}, expected_sha="sha"
        )
        assert not proof.is_green

    def test_missing_duplicate_extra_and_wrong_version_withhold_proof(self) -> None:
        expected = {"a": "v1", "b": "v2"}
        good = [_pass("a"), _pass("b")]
        good[0]["version"] = "v1"
        good[1]["version"] = "v2"
        for rows in (good[:1], [good[0], good[0]], [*good, _pass("c")], [good[0], {**good[1], "version": "old"}]):
            proof = evaluate_green_proof(_payload(rows, _totals(rows)), expected=expected, expected_sha="sha")
            assert not proof.is_green

    def test_zero_executions_cannot_prove_nonempty_selection(self) -> None:
        proof = evaluate_green_proof(_payload([], _totals([])), expected={"a": "v1"}, expected_sha="sha")
        assert not proof.is_green


class TestUnclassifiedRowsAreGating:
    def test_a_row_with_no_triage_class_key_is_a_red(self) -> None:
        # `None` means the grader classified it as a pass; an ABSENT key means no
        # grader this gate understands wrote the row, which is not the same fact.
        scenarios: list[dict[str, object]] = [{"name": "a", "lane": "clean_room", "verdict": "pass"}]
        proof = evaluate_green_proof(_payload(scenarios, _totals(scenarios)), expected={"a": "v"}, expected_sha="sha")
        assert not proof.is_green
        assert [r.triage_class for r in proof.reds] == [UNCLASSIFIED]

    def test_a_non_mapping_row_is_a_red(self) -> None:
        payload: dict[str, object] = {
            "totals": {"total": 1, "passed": 1, "failed": 0, "skipped": 0},
            "scenarios": ["not-a-row"],
        }
        proof = evaluate_green_proof(payload, expected={"a": "v"}, expected_sha="sha")
        assert not proof.is_green

    def test_an_advisory_row_missing_its_triage_class_still_gates(self) -> None:
        # The advisory exemption is an explicit opt-in the producer writes; it can
        # never rescue a row whose grading is unknown.
        scenarios: list[dict[str, object]] = [{"name": "a", "lane": "clean_room", "advisory": True}]
        proof = evaluate_green_proof(_payload(scenarios, _totals(scenarios)), expected={"a": "v"}, expected_sha="sha")
        assert not proof.is_green
