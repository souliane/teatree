"""No shipped surface names a control this branch retired, except to say it is gone (D3).

The sweep that moved every instruction onto ``t3 loop preset use`` was run by
grep, by hand, twice, and missed lines both times — including the two ends of
``t3 review approve --help``, which is the one surface an operator reads at the
moment they are blocked. A miss is invisible: the retired key still WRITES (
``ConfigSetting.set_value`` does not refuse a removed key), so the agent follows
the instruction, the resolver drops the row, and nothing reports that the posture
never moved.

Markdown alone was not enough twice over. It DOES reach the CLI help those two
lines lived in — ``docs/generated/cli-reference.md`` is generated from the command
docstrings and held to them by the "CLI reference doc in sync" hook. It does not
reach a Python docstring or comment, and the retired vocabulary moved there: several
suites went on describing the retired dial's values as live behaviour while every
assertion under them staged a posture. That is the same defect one surface over, so
the scan reads the PROSE of a ``.py`` file — its docstrings and comments, never its
code, where the retired names are legitimately the subject under test.

Naming a retired key to say it IS retired is the point of the retirement notice, so
those lines are ENUMERATED in ``retired_control_notice_lines.txt``. A keyword marker
was tried first and is not enough: "removed", "no longer" and "retired" all read
naturally inside an instruction, so the marker exempts the very lines the scan exists
to catch — the three shapes parametrized below all graded EXEMPT under it.

The reach is SPELLINGS, not meanings, and the title's "names" should be read that way.
:data:`_RETIRED_CONTROL_RE` matches the identifier and its backticked forms; a line that
says the same thing in plain English — the dial's words spaced or hyphenated apart, or a
value named without the key — is not reached, and neither is a claim that is false about
the posture without naming the dial at all. Widening it is not free: the English words
this would have to match are ordinary ones, and a false positive here retires the gate's
usefulness rather than the dial's. So the scope is deliberate, and the class it cannot
cover is the reviewer's, not this file's.
"""

import ast
import io
import re
import tokenize
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Markdown an operator or an agent reads for instruction. The ledger
#: (``config/retired_settings_ledger.py``) is data, not prose, and is not scanned.
_PROSE_ROOTS = ("skills", "docs")

#: Python whose docstrings and comments describe behaviour to whoever maintains it.
_CODE_ROOTS = ("src", "tests")

#: The retired on-behalf dial and its three values, in the forms prose spells them.
#: ``ask`` alone is not a token — the English word is everywhere — so the two
#: distinguishable values carry the check.
_RETIRED_CONTROL_RE = re.compile(
    r"(?:``|`)(?:immediate|draft_or_ask|on_behalf_post_mode|ask_before_post_on_behalf)(?:``|`)"
    r"|\bon_behalf_post_mode\b|\bask_before_post_on_behalf\b|\bdraft_or_ask\b"
)

#: The enumerated notice lines, verbatim and stripped — the only exemption there is.
_NOTICE_ALLOWLIST_PATH = Path(__file__).with_name("retired_control_notice_lines.txt")

_DOCSTRING_HOLDERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _scanned_files() -> list[Path]:
    markdown = [p for root in _PROSE_ROOTS for p in sorted((_REPO_ROOT / root).rglob("*.md"))]
    python = [p for root in _CODE_ROOTS for p in sorted((_REPO_ROOT / root).rglob("*.py"))]
    return markdown + sorted(_REPO_ROOT.glob("*.md")) + python


def prose_of(path: Path) -> str:
    """The human-readable half of *path* — every line for markdown, docstrings + comments for Python."""
    source = path.read_text(encoding="utf-8")
    if path.suffix != ".py":
        return source
    tree = ast.parse(source)
    docstrings = [
        doc for node in ast.walk(tree) if isinstance(node, _DOCSTRING_HOLDERS) and (doc := ast.get_docstring(node))
    ]
    # The marker is stripped because the allowlist file spells its OWN comments with ``#``,
    # so a ``#``-leading entry could never be enumerated there.
    comments = [
        token.string.lstrip("#").strip()
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
    ]
    return "\n".join(docstrings + comments)


def notice_allowlist() -> frozenset[str]:
    lines = _NOTICE_ALLOWLIST_PATH.read_text(encoding="utf-8").splitlines()
    return frozenset(line.strip() for line in lines if line.strip() and not line.startswith("#"))


def lines_naming_the_retired_control(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if _RETIRED_CONTROL_RE.search(line)]


def instructional_violations(text: str) -> list[str]:
    """Lines naming a retired control that the notice allowlist does not enumerate."""
    return [line for line in lines_naming_the_retired_control(text) if line not in notice_allowlist()]


def test_no_shipped_surface_names_the_retired_on_behalf_dial() -> None:
    offenders = {
        f"{path.relative_to(_REPO_ROOT)}: {line}"
        for path in _scanned_files()
        for line in instructional_violations(prose_of(path))
    }

    assert not offenders, (
        "a shipped surface still names a retired on-behalf control — fix the line, or, if it is a "
        f"retirement notice, add it verbatim to {_NOTICE_ALLOWLIST_PATH.name}:\n" + "\n".join(sorted(offenders))
    )


class TestTheScanCanTellAnInstructionFromANotice:
    """The control: a green scan is only evidence once both polarities are proved."""

    def test_it_flags_the_help_tail_that_shipped_twice(self) -> None:
        assert instructional_violations("to satisfy the gate without switching mode to\n`immediate`.\n") == [
            "`immediate`."
        ]

    def test_it_flags_a_retired_value_named_as_the_shipped_default(self) -> None:
        assert instructional_violations("on the shipped `draft_or_ask` it is WITHHELD") != []

    def test_the_allowlist_is_the_only_thing_exempting_the_shipped_notices(self) -> None:
        """Every line reaching the scan is an enumerated notice — nothing is exempt by inference."""
        named = {line for path in _scanned_files() for line in lines_naming_the_retired_control(prose_of(path))}

        assert named, "no shipped line names the control at all — the allowlist would prove nothing"
        assert named == notice_allowlist()

    @pytest.mark.parametrize(
        "line",
        [
            "Set `on_behalf_post_mode` to `immediate` — the old ask flow is removed.",
            "`draft_or_ask` is no longer needed; set it to `immediate` before you post.",
            "run `t3 config_setting set on_behalf_post_mode immediate` (the retired preset path)",
        ],
    )
    def test_an_instruction_carrying_a_retirement_word_is_still_an_instruction(self, line: str) -> None:
        """The exemption is the enumerated notice, never a keyword an instruction can also carry."""
        assert instructional_violations(line) == [line]

    def test_a_python_code_line_naming_the_dial_is_not_prose(self, tmp_path: Path) -> None:
        """The retired names are legitimately the subject under test — only the prose is scanned."""
        module = tmp_path / "probe.py"
        module.write_text('KEY = "on_behalf_post_mode"\n', encoding="utf-8")

        assert instructional_violations(prose_of(module)) == []

    @pytest.mark.parametrize(
        "source",
        ['"""Under `draft_or_ask` it is withheld."""', "# Under `draft_or_ask` it is withheld."],
    )
    def test_a_python_docstring_or_comment_naming_the_dial_is_prose(self, tmp_path: Path, source: str) -> None:
        module = tmp_path / "probe.py"
        module.write_text(f"{source}\n", encoding="utf-8")

        assert instructional_violations(prose_of(module)) == ["Under `draft_or_ask` it is withheld."]

    def test_the_english_word_is_not_the_retired_value(self) -> None:
        assert instructional_violations("a missing credential fails immediately instead of hanging") == []
