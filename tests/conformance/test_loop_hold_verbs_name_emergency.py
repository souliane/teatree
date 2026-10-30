"""A bare `t3 loop resume|disable|pause <name>` exits 2, so guidance naming it sends the reader into a refusal."""

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCANNED = ("src", "skills", "agents", "docs", "README.md", "BLUEPRINT.md")
BARE_VERB = re.compile(r"t3 loop (?:resume|disable|pause)(?:[|/]\w+)* [^-.\s][^`\"'\n]*")


def _prose_and_source() -> Iterator[Path]:
    for root in SCANNED:
        files = (REPO / root).rglob("*") if (REPO / root).is_dir() else [REPO / root]
        yield from (f for f in files if f.suffix in {".py", ".md"} and "generated" not in f.relative_to(REPO).parts)


def _bare_spans(line: str) -> list[str]:
    return [span.group(0) for span in BARE_VERB.finditer(line) if "--emergency" not in span.group(0)]


@pytest.mark.parametrize(
    ("bare", "fixed"),
    [
        ("`t3 loop pause <name>`", "`t3 loop pause <name> --emergency`"),
        ("`t3 loop disable dispatch`", "`t3 loop disable dispatch --emergency`"),
        ("`t3 loop pause <typo>`", "`t3 loop pause <typo> --emergency`"),
        ("`t3 loop resume|disable <name>`", "`t3 loop resume|disable <name> --emergency`"),
        (
            "unmask the loop row: t3 loop resume directive_loop",
            "unmask the loop row: t3 loop resume directive_loop --emergency",
        ),
    ],
)
def test_the_guard_refuses_a_hold_verb_until_it_names_the_emergency_flag(bare: str, fixed: str) -> None:
    assert _bare_spans(bare)
    assert not _bare_spans(fixed)


def test_guidance_naming_a_loop_hold_verb_passes_the_emergency_flag() -> None:
    bare = [
        f"{path.relative_to(REPO)}:{number}: {span}"
        for path in _prose_and_source()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        for span in _bare_spans(line)
    ]

    assert not bare, "\n".join(bare)
