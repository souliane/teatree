"""A bare `t3 loop resume|disable|pause <name>` exits 2, so guidance naming it sends the reader into a refusal."""

import re
from collections.abc import Iterator
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCANNED = ("src", "skills", "agents", "docs", "README.md", "BLUEPRINT.md")
BARE_VERB = re.compile(r"(?:`|^\s*)t3 loop (?:resume|disable|pause)(?:[|/]\w+)* (?:<name>|\{\w+\}|X)[^`\n]*")


def _prose_and_source() -> Iterator[Path]:
    for root in SCANNED:
        files = (REPO / root).rglob("*") if (REPO / root).is_dir() else [REPO / root]
        yield from (f for f in files if f.suffix in {".py", ".md"} and "generated" not in f.relative_to(REPO).parts)


def test_guidance_naming_a_loop_hold_verb_passes_the_emergency_flag() -> None:
    bare = [
        f"{path.relative_to(REPO)}:{number}: {span.group(0)}"
        for path in _prose_and_source()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        for span in BARE_VERB.finditer(line)
        if "--emergency" not in span.group(0)
    ]

    assert not bare, "\n".join(bare)
