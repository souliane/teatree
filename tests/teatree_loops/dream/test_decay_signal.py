"""The budget projection's "EXACT" claim, made falsifiable.

``decay_signal`` projects the post-archival index size arithmetically rather than
by calling the renderer, and a comment asserts the arithmetic is exact against
``render_index_lines(survivor lines)`` for any count. Nothing checked that. An
off-by-one in either direction silently mis-times the archive stop condition —
too eager and it archives a memory the budget would have kept, too lax and the
nightly gate fails with no diagnostic — and neither shows up as a test failure.

This is the falsification: render the real thing at several survivor counts, with
and without a priority preamble, and compare against the same arithmetic the
projection uses.
"""

from pathlib import Path

import pytest

from teatree.loops.dream import reindex

_PREAMBLE = "# Memory — priority\n\nRead these first.\n"


def _projected(header: str, line_bytes: list[int]) -> tuple[int, int]:
    """The projection's own arithmetic, verbatim from ``decay_signal``."""
    survivor_count = len(line_bytes)
    return (
        len(header.encode("utf-8")) + sum(line_bytes) + survivor_count,
        len(header.splitlines()) + survivor_count,
    )


@pytest.mark.parametrize("survivors", [0, 1, 2, 5, 17])
@pytest.mark.parametrize("preamble", ["", _PREAMBLE])
def test_the_projection_is_byte_and_line_exact_against_the_renderer(survivors: int, preamble: str) -> None:
    names = [f"memory-{index:03d}.md" for index in range(survivors)]
    lines = [reindex.index_line_for(name) for name in names]

    rendered = reindex.render_index_lines(lines, preamble)
    header = reindex.render_index_lines([], preamble)
    projected_bytes, projected_lines = _projected(header, [len(line.encode("utf-8")) for line in lines])

    assert projected_bytes == len(rendered.encode("utf-8"))
    assert projected_lines == len(rendered.splitlines())


def test_a_multibyte_memory_name_is_counted_in_bytes_not_characters(tmp_path: Path) -> None:
    # The budget is a BYTE budget, so a non-ASCII filename must not be undercounted.
    lines = [reindex.index_line_for("mémoire-café.md")]
    rendered = reindex.render_index_lines(lines, "")
    header = reindex.render_index_lines([], "")

    projected_bytes, _ = _projected(header, [len(line.encode("utf-8")) for line in lines])

    assert projected_bytes == len(rendered.encode("utf-8"))
