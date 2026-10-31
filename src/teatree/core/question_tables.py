"""The plain tables ``questions list`` and ``questions reachability`` print."""

import io
from typing import TypedDict

from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.table_output import print_table


class QuestionReachRow(TypedDict):
    """One row of ``t3 <overlay> questions reachability --json``."""

    id: int
    has_subject: bool
    resolvers: dict[str, str]


def render_reachability_table(rows: list["QuestionReachRow"]) -> str:
    buffer = io.StringIO()
    unreachable = sum(1 for row in rows if not row["resolvers"])
    print_table(
        ["ID", "Subject", "Resolvers"],
        [
            [
                row["id"],
                "yes" if row["has_subject"] else "no",
                ", ".join(f"{name}={verdict}" for name, verdict in sorted(row["resolvers"].items())) or "—",
            ]
            for row in rows
        ],
        title=f"{len(rows)} pending, {unreachable} reachable by no resolver",
        stream=buffer,
        justify=["right", "left", "left"],
    )
    return buffer.getvalue()


def render_questions_table(rows: list[DeferredQuestion]) -> str:
    buffer = io.StringIO()
    escalated = sum(1 for row in rows if row.escalated_at is not None)
    table_rows = [
        [
            row.pk,
            row.status,
            f"{row.escalation_count}x" if row.escalated_at is not None else "—",
            row.created_at.isoformat() if row.created_at is not None else "?",
            row.question,
            row.answer_text,
        ]
        for row in rows
    ]
    title = f"{len(rows)} open question(s)"
    if escalated:
        title += f", {escalated} past the age ceiling"
    print_table(
        ["ID", "Status", "Escalated", "Created", "Question", "Answer"],
        table_rows,
        title=title,
        stream=buffer,
        justify=["right", "left", "right", "left", "left", "left"],
    )
    return buffer.getvalue()
