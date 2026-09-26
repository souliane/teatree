"""Read a test run's outcome buckets off their SECTION LABELS, never off position.

A Playwright epilogue states each outcome as a count and a label, then indents
that bucket's test names underneath::

    3 failed
        <one indented line per failing test>
    2 flaky
        <one indented line per flaky test>

The label is the only thing that says which bucket a name is in. Reading "the
numbered entries" or "the block near the top" returns whichever bucket the
reporter happened to print first, which is how one run's failed and flaky sets
were reported inverted — a passing scenario named as broken while the one that
actually failed went unmentioned.

So a parsed run is reachable only by label: :class:`RunSummary` holds a mapping,
never a sequence, and every reader on it is a name. A label the summary never
declared reads as an empty :class:`Outcome` rather than raising, so an absent
bucket and a zero-sized one answer alike.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass

PASSED = "passed"
FAILED = "failed"
FLAKY = "flaky"
SKIPPED = "skipped"
DID_NOT_RUN = "did not run"
INTERRUPTED = "interrupted"

_LABELS = (FAILED, INTERRUPTED, FLAKY, SKIPPED, DID_NOT_RUN, PASSED)

_HEADER = re.compile(rf"^(?P<indent>\s*)(?P<count>\d+)\s+(?P<label>{'|'.join(map(re.escape, _LABELS))})\b")
#: The reporter pads each entry to the terminal width with a box-drawing rule.
_ENTRY_PADDING = re.compile(r"\s*─+\s*$")


@dataclass(frozen=True, slots=True)
class Outcome:
    """One labelled bucket: the count the summary declared, and the names it listed.

    ``names`` is shorter than ``count`` for the buckets Playwright counts without
    detailing — ``passed`` and ``skipped`` get a number and no list.
    """

    count: int = 0
    names: tuple[str, ...] = ()


_ABSENT = Outcome()


@dataclass(frozen=True, slots=True)
class RunSummary:
    """The outcome buckets of one run, keyed by the label that declared them."""

    by_label: Mapping[str, Outcome]

    @property
    def passed(self) -> Outcome:
        return self.by_label.get(PASSED, _ABSENT)

    @property
    def failed(self) -> Outcome:
        return self.by_label.get(FAILED, _ABSENT)

    @property
    def flaky(self) -> Outcome:
        return self.by_label.get(FLAKY, _ABSENT)

    @property
    def skipped(self) -> Outcome:
        return self.by_label.get(SKIPPED, _ABSENT)

    @property
    def did_not_run(self) -> Outcome:
        return self.by_label.get(DID_NOT_RUN, _ABSENT)

    @property
    def interrupted(self) -> Outcome:
        return self.by_label.get(INTERRUPTED, _ABSENT)


def read_playwright_summary(text: str) -> RunSummary:
    """Parse a Playwright epilogue into its labelled buckets."""
    counts: dict[str, int] = {}
    names: dict[str, list[str]] = {}
    label: str | None = None
    header_indent = 0

    for line in text.splitlines():
        header = _HEADER.match(line)
        if header:
            label = header["label"]
            header_indent = len(header["indent"])
            counts[label] = int(header["count"])
            names.setdefault(label, [])
            continue
        entry = line.strip()
        if label is None or not entry:
            continue
        # A section ends at the first non-blank line back at or left of its header's column.
        if len(line) - len(line.lstrip()) <= header_indent:
            label = None
            continue
        names[label].append(_ENTRY_PADDING.sub("", entry))

    return RunSummary(by_label={found: Outcome(counts[found], tuple(names[found])) for found in counts})
