"""Rule-clause preservation: a RULES.md edit may not drop, reword or move a clause unless its removal is named."""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from teatree.utils import git

RULES_PATH = "skills/rules/RULES.md"
REMOVAL_TRAILER = "Rule-clause-removed:"
RUBRIC_ROW = "every clause of the old rule is still present or its removal is named"

_MIN_FRAGMENT_LENGTH = 12
_TRAILER_RE = re.compile(rf"^{re.escape(REMOVAL_TRAILER)}[ \t]*(.+?)[ \t]*$", re.MULTILINE | re.IGNORECASE)
_HEADING_RE = re.compile(r"^#+\s+(.*)$")
_LIST_MARKER_RE = re.compile(r"^(?:\d+\.|[-*])\s+")
_TITLE_RE = re.compile(r"^\*\*(.+?)\*\*\s*")
_SENTENCE_END_RE = re.compile(r"(?<=[.!?;])\s+")


@dataclass(frozen=True, slots=True)
class RuleClause:
    entry: str
    text: str


@dataclass(frozen=True, slots=True)
class RulesDiff:
    touched: bool
    missing: tuple[RuleClause, ...]

    def refusal(self) -> str | None:
        if not self.missing:
            return None
        listing = "\n".join(f"  - [{clause.entry}] {clause.text}" for clause in self.missing)
        return (
            f"{RULES_PATH} drops, rewords or moves {len(self.missing)} clause(s) that no "
            f"{REMOVAL_TRAILER} commit trailer names:\n{listing}\n"
            f"Keep each clause verbatim, or add a `{REMOVAL_TRAILER} <clause text or rule title>` trailer per removal."
        )


def _normalise(text: str) -> str:
    return " ".join(text.split())


def _entry_and_body(line: str, heading: str) -> tuple[str, str]:
    body = _LIST_MARKER_RE.sub("", line, count=1)
    title = _TITLE_RE.match(body)
    if title is None:
        return heading, body
    return title.group(1).strip().removesuffix(":").strip(), body[title.end() :]


def rule_clauses(text: str) -> tuple[RuleClause, ...]:
    heading = ""
    clauses: list[RuleClause] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if matched := _HEADING_RE.match(line):
            heading = matched.group(1).strip()
            continue
        entry, body = _entry_and_body(line, heading)
        clauses.extend(
            RuleClause(entry, sentence)
            for sentence in (_normalise(part) for part in _SENTENCE_END_RE.split(body))
            if sentence
        )
    return tuple(clauses)


def _names(clause: RuleClause, name: str) -> bool:
    text = clause.text.casefold()
    return name in {clause.entry.casefold(), text} or (len(name) >= _MIN_FRAGMENT_LENGTH and name in text)


def missing_clauses(old_text: str, new_text: str, named_removals: Sequence[str]) -> tuple[RuleClause, ...]:
    kept = set(rule_clauses(new_text))
    names = [folded for name in named_removals if (folded := _normalise(name).casefold())]
    return tuple(
        clause
        for clause in rule_clauses(old_text)
        if clause not in kept and not any(_names(clause, name) for name in names)
    )


def removal_names(commit_messages: Sequence[str]) -> tuple[str, ...]:
    return tuple(found.group(1) for message in commit_messages for found in _TRAILER_RE.finditer(message))


def _text_at(repo: str, ref: str, path: str) -> str:
    if not git.check(repo=repo, args=["cat-file", "-e", f"{ref}:{path}"]):
        return ""
    return git.run_strict_verbatim(repo=repo, args=["show", f"{ref}:{path}"])


def rules_diff(repo: str, *, base: str, head: str, path: str = RULES_PATH) -> RulesDiff:
    """The clauses *head* drops from *path* relative to the merge-base with *base*; raises when git cannot say."""
    fork = git.run_strict(repo=repo, args=["merge-base", base, head])
    if not git.run_strict(repo=repo, args=["diff", "--name-only", fork, head, "--", path]):
        return RulesDiff(touched=False, missing=())
    names = removal_names(git.commit_messages(repo, f"{fork}..{head}"))
    return RulesDiff(
        touched=True,
        missing=missing_clauses(_text_at(repo, fork, path), _text_at(repo, head, path), names),
    )
