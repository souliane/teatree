"""No retired Ticket FSM name reappears outside the code that must keep them (#4779).

The rename has no dual-read shim, so a stale name in code writes an orphaned row, and a
stale name in docs, skills or agent prompts teaches an agent a state that no longer
exists. ``started``/``planned``/``reviewed``/``shipped`` are ordinary English, so they
are only matched in code-shaped forms: a backticked or compared/assigned/keyed quoted
literal, a ``State.X`` member or ``State("x")`` call, or either end of an FSM arrow. The
three distinctive names are matched anywhere.
"""

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCAN_DIRS = ("src", "docs", "skills", "agents", "hooks", "e2e", "tests")
_SCAN_ROOT_FILES = ("README.md", "AGENTS.md", "BLUEPRINT.md", "CLAUDE.md")
_SCAN_SUFFIXES = frozenset({".py", ".md", ".html", ".txt", ".yaml", ".yml", ".toml", ".json", ".sh"})
_MIGRATIONS = _REPO_ROOT / "src" / "teatree" / "core" / "migrations"

#: Files whose job is to hold retired values: they seed or assert pre-rename rows.
_HOLDS_RETIRED_VALUES = frozenset(
    {
        "tests/quality/test_no_retired_ticket_state_names.py",
        "tests/teatree_core/test_rename_ticket_fsm_states_migration.py",
        "tests/teatree_core/test_rehome_reviewer_delivered_migration.py",
        "tests/teatree_cli/doctor/test_unknown_ticket_state_check.py",
    }
)

_AMBIGUOUS = r"started|planned|reviewed|shipped"
_QUOTED = r"[\"'](?:started|planned|reviewed|shipped|in_review|review_posted|retrospected)[\"']"
_STATE_FIELD = r"(?:\b|(?<=_))state"
RETIRED_NAME = re.compile(
    r"\b(?:in_review|review_posted|retrospected|IN_REVIEW|REVIEW_POSTED|RETROSPECTED)\b"
    r"|\bState\.(?:STARTED|PLANNED|REVIEWED|SHIPPED)\b"
    rf"|\bState\(\s*{_QUOTED}\s*\)"
    rf"|`(?:{_AMBIGUOUS})`"
    rf"|{_STATE_FIELD}\s*=\s*{_QUOTED}"
    rf"|{_STATE_FIELD}__in\s*=\s*[\[({{][^\])}}]*?{_QUOTED}"
    rf"|[!=]=\s*{_QUOTED}|{_QUOTED}\s*[!=]="
    rf"|{_QUOTED}\s*:"
    r"|\b(?:STARTED|PLANNED|REVIEWED|SHIPPED)\s*(?:→|-->|->)"
    r"|(?:→|-->|->)\s*(?:STARTED|PLANNED|REVIEWED|SHIPPED)\b"
)

#: (file, match, phrase on that line) for words that name something other than a Ticket state.
_NOT_A_TICKET_STATE = frozenset(
    {
        ("BLUEPRINT.md", "`shipped`", "OUTCOME_CLAIM_KINDS"),
        ("src/teatree/config/gate_evidence.py", "`shipped`", "date is the day"),
        ("src/teatree/core/merge/conflict_only.py", "`reviewed`", "FIRST parent is exactly"),
        ("src/teatree/docker/reclaim.py", "`planned`", "steps carry the exact argv"),
        ("src/teatree/core/review/completion_evidence.py", '"shipped":', '"shipped": "shipped"'),
        ("tests/teatree_core/review/test_completion_evidence.py", '== "shipped"', "claim_kind"),
        ("tests/test_block_uncovered_diff_hook.py", "`shipped`", "SESSION repo"),
    }
)


def _scanned_files() -> list[Path]:
    files = [_REPO_ROOT / name for name in _SCAN_ROOT_FILES if (_REPO_ROOT / name).is_file()]
    for rel in _SCAN_DIRS:
        files.extend(
            path
            for path in (_REPO_ROOT / rel).rglob("*")
            if path.suffix in _SCAN_SUFFIXES
            and path.is_file()
            and not path.is_relative_to(_MIGRATIONS)
            and path.relative_to(_REPO_ROOT).as_posix() not in _HOLDS_RETIRED_VALUES
        )
    return files


def _allowed(rel: str, token: str, line: str) -> bool:
    return any(rel == path and token == match and phrase in line for path, match, phrase in _NOT_A_TICKET_STATE)


def retired_name_hits(text: str, *, rel: str) -> list[str]:
    return [
        f"{rel}:{lineno}: {match.group(0)!r}"
        for lineno, line in enumerate(text.splitlines(), start=1)
        for match in RETIRED_NAME.finditer(line)
        if not _allowed(rel, match.group(0), line)
    ]


def test_no_retired_ticket_state_name_outside_migrations() -> None:
    hits = [
        hit
        for path in _scanned_files()
        for hit in retired_name_hits(
            path.read_text(encoding="utf-8", errors="replace"), rel=path.relative_to(_REPO_ROOT).as_posix()
        )
    ]
    assert not hits, "retired Ticket state names are back:\n" + "\n".join(hits)


@pytest.mark.parametrize(
    "planted",
    [
        "ticket.state = 'in_review'",
        "held until review_posted",
        "Ticket.State.SHIPPED",
        "State.STARTED",
        "sits at ``planned``",
        "filter(state__in=['reviewed'])",
        "STARTED → PLANNED",
        'if ticket.state == "shipped":',
        'while state != "planned"',
        'if "reviewed" == ticket.state',
        'filter(state__in=["coded", "started"])',
        'TicketTransitionFactory(from_state="shipped")',
        "TicketTransition(to_state='reviewed')",
        'Ticket.State("shipped")',
        '_PHASE = {"started": "planning"}',
    ],
)
def test_the_pattern_catches_each_retired_form(planted: str) -> None:
    assert len(retired_name_hits(planted, rel="planted.py")) == 1


@pytest.mark.parametrize(
    "prose",
    [
        "the agent started, planned, reviewed and shipped it",
        "Once started, the sweep reviewed what shipped.",
        'print("started")',
        'phase = "shipping"',
        'state = "work_started"',
        'filter(state__in=["coded", "pr_opened"])',
        'self._claim(verb="started")',
    ],
)
def test_plain_english_and_live_names_are_not_matched(prose: str) -> None:
    assert retired_name_hits(prose, rel="prose.py") == []


def test_an_allowlisted_token_elsewhere_in_the_same_file_is_still_caught() -> None:
    assert retired_name_hits("the ticket sits at `reviewed`", rel="src/teatree/core/merge/conflict_only.py")
