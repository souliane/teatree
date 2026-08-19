"""``ScanSignal`` builders for the pr_sweep scanner — pure construction, no I/O."""

from teatree.loop.scanners.pr_sweep_signals import pass_signal, signal_from_attempt
from teatree.loop.scanners.pr_sweep_types import MergeAttempt


class TestPassSignal:
    def test_kind_and_payload_shape(self) -> None:
        signal = pass_signal(slug="o/r", pr_ids=[6230, 7777], overlay="teatree")

        assert signal.kind == "pr_sweep.pass"
        assert signal.payload == {"slug": "o/r", "pr_ids": [6230, 7777], "overlay": "teatree"}

    def test_summary_names_the_slug_and_count(self) -> None:
        signal = pass_signal(slug="o/r", pr_ids=[1, 2, 3], overlay="")

        assert signal.summary == "o/r pass: 3 open PR(s)"

    def test_empty_pr_ids_still_produces_a_signal(self) -> None:
        signal = pass_signal(slug="o/r", pr_ids=[], overlay="teatree")

        assert signal.payload["pr_ids"] == []


class TestSignalFromAttempt:
    def test_a_merged_attempt_uses_the_merged_kind_regardless_of_decision(self) -> None:
        attempt = MergeAttempt(slug="o/r", pr_id=1, decision="blocked", merged=True, merged_sha="abc")

        signal = signal_from_attempt(attempt, overlay="teatree")

        assert signal.kind == "pr_sweep.merged"

    def test_a_non_merged_attempt_uses_the_decision_as_the_kind_suffix(self) -> None:
        attempt = MergeAttempt(slug="o/r", pr_id=1, decision="skip", reason="draft", url="https://example.test/pr/1")

        signal = signal_from_attempt(attempt, overlay="teatree")

        assert signal.kind == "pr_sweep.skip"
        assert signal.payload["url"] == "https://example.test/pr/1"
        assert signal.payload["reason"] == "draft"
        assert signal.payload["merged"] is False

    def test_held_and_authorizing_verdicts_are_carried_as_lists(self) -> None:
        attempt = MergeAttempt(
            slug="o/r",
            pr_id=1,
            decision="flag_held",
            held_verdicts=((5, "alice"),),
            authorizing_verdict=(6, "bob"),
        )

        signal = signal_from_attempt(attempt, overlay="teatree")

        assert signal.payload["held_verdicts"] == [[5, "alice"]]
        assert signal.payload["authorizing_verdict"] == [6, "bob"]

    def test_no_authorizing_verdict_is_none(self) -> None:
        attempt = MergeAttempt(slug="o/r", pr_id=1, decision="skip", reason="ci_pending")

        signal = signal_from_attempt(attempt, overlay="teatree")

        assert signal.payload["authorizing_verdict"] is None
