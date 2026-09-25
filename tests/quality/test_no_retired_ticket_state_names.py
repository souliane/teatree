"""No retired Ticket FSM name reappears outside the migrations that must keep them (#4779).

The rename has no dual-read shim, so a stale name in code writes an orphaned row, and a
stale name in docs, skills or agent prompts teaches an agent a state that no longer
exists. ``started``/``planned``/``reviewed``/``shipped`` are ordinary English, so they
are only matched in code-shaped forms: a backticked literal, an ORM ``state=`` literal,
a ``State.X`` member, or either end of an FSM arrow. The three distinctive names are
matched anywhere.
"""

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCAN_DIRS = ("src", "docs", "skills", "agents", "hooks", "e2e")
_SCAN_ROOT_FILES = ("README.md", "AGENTS.md", "BLUEPRINT.md", "CLAUDE.md")
_SCAN_SUFFIXES = frozenset({".py", ".md", ".html", ".txt", ".yaml", ".yml", ".toml", ".json", ".sh"})
_MIGRATIONS = _REPO_ROOT / "src" / "teatree" / "core" / "migrations"

_AMBIGUOUS = r"started|planned|reviewed|shipped"
RETIRED_NAME = re.compile(
    r"\b(?:in_review|review_posted|retrospected|IN_REVIEW|REVIEW_POSTED|RETROSPECTED)\b"
    rf"|\bState\.(?:STARTED|PLANNED|REVIEWED|SHIPPED)\b"
    rf"|`(?:{_AMBIGUOUS})`"
    rf"|\bstate(?:__in)?\s*=\s*[\[({{]?\s*[\"'](?:{_AMBIGUOUS})[\"']"
    r"|\b(?:STARTED|PLANNED|REVIEWED|SHIPPED)\s*(?:→|-->|->)"
    r"|(?:→|-->|->)\s*(?:STARTED|PLANNED|REVIEWED|SHIPPED)\b"
)

#: Backticked words that name something other than a Ticket state.
_NOT_A_TICKET_STATE = {
    ("BLUEPRINT.md", "`shipped`"),  # an OUTCOME_CLAIM_KINDS verb
    ("src/teatree/config/gate_evidence.py", "`shipped`"),  # the per-key date field
    ("src/teatree/core/merge/conflict_only.py", "`reviewed`"),  # the reviewed-SHA argument
    ("src/teatree/docker/reclaim.py", "`planned`"),  # a dry-run's planned steps
}


def _scanned_files() -> list[Path]:
    files = [_REPO_ROOT / name for name in _SCAN_ROOT_FILES if (_REPO_ROOT / name).is_file()]
    for rel in _SCAN_DIRS:
        files.extend(
            path
            for path in (_REPO_ROOT / rel).rglob("*")
            if path.suffix in _SCAN_SUFFIXES and path.is_file() and not path.is_relative_to(_MIGRATIONS)
        )
    return files


def retired_name_hits(text: str, *, rel: str) -> list[str]:
    return [
        f"{rel}:{lineno}: {match.group(0)!r}"
        for lineno, line in enumerate(text.splitlines(), start=1)
        for match in RETIRED_NAME.finditer(line)
        if (rel, match.group(0)) not in _NOT_A_TICKET_STATE
    ]


def test_no_retired_ticket_state_name_outside_migrations() -> None:
    hits = [
        hit
        for path in _scanned_files()
        for hit in retired_name_hits(
            path.read_text(encoding="utf-8", errors="replace"), rel=str(path.relative_to(_REPO_ROOT))
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
    ],
)
def test_the_pattern_catches_each_retired_form(planted: str) -> None:
    assert len(retired_name_hits(planted, rel="planted.py")) == 1


def test_the_english_words_are_not_matched() -> None:
    assert retired_name_hits("the agent started, planned, reviewed and shipped it", rel="prose.md") == []
