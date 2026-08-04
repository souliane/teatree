"""Tests for the unbacked-claim detector — a diagnosis or an alarm cites what was read.

Two recorded failures share one shape: fluent prose generated from check NAMES,
carrying the confidence of a diagnosis nobody had read the log for; and a
"SEVERE" escalation raised before the evidence the agent's own brief had asked
for came back. Both are a claim about a system's state with nothing read behind
it, so both are refused by the same evidence requirement.

The pass class is the over-block control: a cited diagnosis, an honestly hedged
one, a severity finding carrying a file:line, and ordinary prose that merely
mentions a failure must all stay silent.
"""

from teatree.hooks.unbacked_claim_scanner import find_unbacked_claim

# The recorded diagnosis, invented from the check names, with nothing quoted.
_FLUENT_DIAGNOSIS = (
    "#4001 failed because 466 files were meeting upstream's gates for the first time, "
    "so the whole contribution tripped the ratchet at once.\n"
)

# The same finding once the log was actually opened.
_CITED_DIAGNOSIS = (
    "#4001 failed because of one real violation the contribution itself introduced:\n"
    "```\n"
    "src/teatree/core/models/ticket.py: 523 LOC, up from 510 (over the 500 cap).\n"
    "Over-cap files may only shrink.\n"
    "```\n"
)

# The recorded alarm: full severity, relayed symptom, settling evidence not back.
_PREMATURE_ALARM = (
    "SEVERE: a selected offer repriced 37.5 bps between the quote and the acceptance.\n"
    "I have asked the lane for the offer ordering and am awaiting its report before I can confirm.\n"
)


class TestFiresOnUnbackedDiagnosis:
    def test_causal_failure_claim_with_no_citation_fires(self) -> None:
        verdict = find_unbacked_claim(_FLUENT_DIAGNOSIS)

        assert verdict is not None
        assert verdict.kind == "diagnosis"
        assert "because" in verdict.claim

    def test_reason_phrasing_with_no_citation_fires(self) -> None:
        text = "The pipeline is red; the reason is that the eval job cannot reach its database.\n"

        verdict = find_unbacked_claim(text)

        assert verdict is not None
        assert verdict.kind == "diagnosis"


class TestFiresOnUnbackedSeverity:
    def test_severity_label_with_no_citation_fires(self) -> None:
        verdict = find_unbacked_claim("SEVERE: a selected offer repriced between quote and acceptance.\n")

        assert verdict is not None
        assert verdict.kind == "severity"

    def test_severity_with_settling_evidence_outstanding_fires_even_when_cited(self) -> None:
        text = _PREMATURE_ALARM + "The assertion that flagged it was `offers[0].rate != selected.rate`.\n"

        verdict = find_unbacked_claim(text)

        assert verdict is not None
        assert verdict.kind == "severity"
        assert any("outstanding" in reason for reason in verdict.missing)


class TestDoesNotFireOnBackedOrHonestTurns:
    def test_cited_diagnosis_passes(self) -> None:
        assert find_unbacked_claim(_CITED_DIAGNOSIS) is None

    def test_diagnosis_citing_a_rule_code_passes(self) -> None:
        text = "The contribution failed because of one real PLR0911 violation in a function it touched.\n"

        assert find_unbacked_claim(text) is None

    def test_diagnosis_citing_a_path_passes(self) -> None:
        text = "The eval job failed because sqlite3.OperationalError: unable to open database file.\n"

        assert find_unbacked_claim(text) is None

    def test_honestly_hedged_diagnosis_passes(self) -> None:
        text = "I have not read the logs yet; my guess is it failed because the ratchet fired.\n"

        assert find_unbacked_claim(text) is None

    def test_severity_finding_with_a_file_line_passes(self) -> None:
        text = "BLOCKER: the null check is dropped at src/teatree/core/models/ticket.py:412.\n"

        assert find_unbacked_claim(text) is None

    def test_prose_mentioning_a_failure_without_a_cause_passes(self) -> None:
        assert find_unbacked_claim("The pipeline failed. I am reading the job log now.\n") is None

    def test_blocked_on_a_dependency_is_a_status_not_a_diagnosis(self) -> None:
        text = "Nothing actionable this tick. The merge is blocked on a human approval that has not landed.\n"

        assert find_unbacked_claim(text) is None

    def test_severity_word_only_inside_a_quoted_log_passes(self) -> None:
        text = "Here is the tail of the run:\n```\nCRITICAL: worker exited\n```\nReading it now.\n"

        assert find_unbacked_claim(text) is None

    def test_empty_input_passes(self) -> None:
        assert find_unbacked_claim("") is None
