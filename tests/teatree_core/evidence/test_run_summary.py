"""The labelled-section contract: a positional read of this summary is WRONG.

``tests/fixtures/playwright_run_summary.txt`` is a Playwright epilogue in the
shape the base reporter emits, with the ``failed`` and ``flaky`` sections
adjacent. ``_positional_read`` is the mis-read that shipped: it slices the same
entry lines by the counts it finds and hands them to the buckets it *expects*, so
the two sets come back inverted — a passing scenario reported as broken, and the
one that actually failed never named. The contrast between the two readers is the
point of this module.
"""

import re
from pathlib import Path

import pytest

from teatree.core.evidence.run_summary import (
    DID_NOT_RUN,
    FAILED,
    FLAKY,
    INTERRUPTED,
    PASSED,
    SKIPPED,
    read_playwright_summary,
)

#: Playwright joins project, ``file:line:col`` and title with this separator.
SEP = "\u203a"
#: The box-drawing rule the reporter pads each entry to the terminal width with.
RULE_CHAR = "\u2500"
RULE = RULE_CHAR * 5

REAL_SUMMARY = (Path(__file__).resolve().parents[2] / "fixtures" / "playwright_run_summary.txt").read_text(
    encoding="utf-8"
)

TRUE_FAILED = (
    f"[chromium] {SEP} renewal/reprice.spec.ts:42:7 {SEP} renewal {SEP} keeps the pinned rate",
    f"[chromium] {SEP} renewal/reprice.spec.ts:88:7 {SEP} renewal {SEP} stores the market rate",
    f"[chromium] {SEP} offers/list.spec.ts:12:5 {SEP} offers {SEP} renders the offer list",
)
TRUE_FLAKY = (
    f"[chromium] {SEP} offers/pdf.spec.ts:30:5 {SEP} offers {SEP} downloads the offer pdf",
    f"[chromium] {SEP} login/sso.spec.ts:9:3 {SEP} login {SEP} signs in through sso",
)

#: The bucket order the mis-reading agent believed the reporter uses.
_ASSUMED_ORDER = (FLAKY, FAILED)


def _positional_read(summary: str) -> dict[str, tuple[str, ...]]:
    """Slice the listed entries by the counts, in an ASSUMED bucket order.

    Padding is stripped exactly as the real reader strips it, so the only thing
    that differs between the two readers is whether the label decides the bucket.
    """
    counts = [int(n) for n in re.findall(rf"^\s*(\d+)\s+(?:{FAILED}|{FLAKY})\b", summary, re.MULTILINE)]
    entries = [
        re.sub(f"\\s*{RULE_CHAR}+\\s*$", "", line.strip())
        for line in summary.splitlines()
        if line.strip().startswith("[")
    ]
    sliced: dict[str, tuple[str, ...]] = {}
    start = 0
    for label, count in zip(_ASSUMED_ORDER, counts, strict=True):
        sliced[label] = tuple(entries[start : start + count])
        start += count
    return sliced


class TestPositionalReadingIsTheBug:
    def test_a_positional_read_of_this_summary_inverts_failed_and_flaky(self) -> None:
        positional = _positional_read(REAL_SUMMARY)
        summary = read_playwright_summary(REAL_SUMMARY)

        assert positional[FAILED] == summary.flaky.names, "the control no longer reproduces the mis-read"
        assert positional[FLAKY] == summary.failed.names, "the control no longer reproduces the mis-read"
        assert positional[FAILED] != summary.failed.names

    def test_the_labelled_read_names_the_scenarios_that_actually_failed(self) -> None:
        summary = read_playwright_summary(REAL_SUMMARY)

        assert summary.failed.names == TRUE_FAILED
        assert summary.flaky.names == TRUE_FLAKY


class TestEveryBucketIsReachableOnlyByItsLabel:
    @pytest.mark.parametrize(
        ("label", "count"),
        [(FAILED, 3), (FLAKY, 2), (SKIPPED, 1), (PASSED, 55)],
    )
    def test_each_declared_bucket_carries_its_own_count(self, label: str, count: int) -> None:
        assert read_playwright_summary(REAL_SUMMARY).by_label[label].count == count

    def test_a_bucket_the_summary_never_mentions_reads_as_empty(self) -> None:
        summary = read_playwright_summary(REAL_SUMMARY)

        assert summary.did_not_run.count == 0
        assert summary.did_not_run.names == ()
        assert summary.interrupted.names == ()

    def test_passed_and_skipped_carry_a_count_the_reporter_lists_no_names_for(self) -> None:
        summary = read_playwright_summary(REAL_SUMMARY)

        assert summary.passed.names == ()
        assert summary.skipped.names == ()

    def test_the_html_report_trailer_below_the_epilogue_lands_in_no_bucket(self) -> None:
        # Without the dedent ending the section, `npx playwright show-report` reads as a passing test.
        buckets = read_playwright_summary(REAL_SUMMARY).by_label.values()

        assert [name for bucket in buckets for name in bucket.names] == [*TRUE_FAILED, *TRUE_FLAKY]

    def test_failure_detail_above_the_epilogue_is_not_mistaken_for_an_entry(self) -> None:
        # The numbered failure header and its stack trace sit above the epilogue and
        # belong to no bucket; only lines indented under a label are entries.
        assert len(read_playwright_summary(REAL_SUMMARY).failed.names) == 3

    def test_reordering_the_sections_does_not_move_a_single_name(self) -> None:
        reordered = "\n".join(
            [
                "  2 flaky",
                *(f"    {name} {RULE}" for name in TRUE_FLAKY),
                "  3 failed",
                *(f"    {name} {RULE}" for name in TRUE_FAILED),
                "  55 passed (2.7m)",
            ]
        )
        summary = read_playwright_summary(reordered)

        assert summary.failed.names == TRUE_FAILED
        assert summary.flaky.names == TRUE_FLAKY

    def test_the_two_word_label_is_read_as_one_bucket(self) -> None:
        summary = read_playwright_summary("  4 did not run\n  1 interrupted\n  9 passed (3s)\n")

        assert summary.by_label[DID_NOT_RUN].count == 4
        assert summary.by_label[INTERRUPTED].count == 1

    def test_an_interrupted_run_is_not_folded_into_failed_or_did_not_run(self) -> None:
        summary = read_playwright_summary(
            f"  1 failed\n    one {RULE}\n  1 interrupted\n    two {RULE}\n",
        )

        assert summary.failed.names == ("one",)
        assert summary.interrupted.names == ("two",)
        assert summary.did_not_run.count == 0

    def test_an_empty_run_reads_as_every_bucket_empty(self) -> None:
        summary = read_playwright_summary("")

        assert summary.by_label == {}
        assert summary.passed.count == 0
        assert summary.failed.names == ()
