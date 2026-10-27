from teatree.agents.review_nit_policy import normalize_review_envelope


def test_nit_only_hold_becomes_merge_safe() -> None:
    envelope = {"verdict": "hold", "findings": [{"severity": "minor", "kind": "nit", "summary": "name"}]}

    normalized = normalize_review_envelope(envelope, None)

    assert normalized["verdict"] == "merge_safe"
    assert normalized["findings"][0]["kind"] == "nit"


def test_blocking_finding_overrides_merge_safe() -> None:
    envelope = {
        "verdict": "merge_safe",
        "findings": [{"severity": "major", "summary": "wrong result", "reproduction": "pytest result: failed"}],
    }

    assert normalize_review_envelope(envelope, None)["verdict"] == "hold"


def test_explicit_note_does_not_turn_merge_safe_into_hold() -> None:
    envelope = {
        "verdict": "merge_safe",
        "findings": [{"severity": "low", "kind": "note", "basis": "executed", "summary": "context"}],
    }

    normalized = normalize_review_envelope(envelope, None)

    assert normalized["verdict"] == "merge_safe"
    assert normalized["findings"][0]["kind"] == "note"


def test_a_red_check_keeps_a_nit_only_hold() -> None:
    envelope = {
        "verdict": "hold",
        "gh_verify_result": "failed",
        "findings": [{"severity": "minor", "kind": "nit", "summary": "name"}],
    }

    assert normalize_review_envelope(envelope, None)["verdict"] == "hold"


def test_missing_findings_keep_the_original_verdict() -> None:
    envelope = {"verdict": "hold"}

    assert normalize_review_envelope(envelope, None) is envelope
