"""Every read-only phase's real system context fits the executor budget with production-sized skills."""

from dataclasses import fields

from django.test import TestCase

from teatree.agents.context_budget import MAX_APPEND_BYTES
from teatree.agents.read_only_draft_facts import DRAFT_FACTS_CAP_BYTES
from teatree.agents.read_only_venue_brief import (
    HANDOFF_CAP_BYTES,
    ISSUE_BODY_CAP_BYTES,
    ISSUE_COMMENTS_CAP_BYTES,
    READ_ONLY_DIRECTIVE,
    REVIEW_CHECKS_CAP_BYTES,
    REVIEW_DIFF_CAP_BYTES,
    SURVEY_CAP_BYTES,
    TICKET_CONTEXT_CAP_BYTES,
    ReadOnlyFacts,
    fit_read_only_context,
    read_only_system_context,
)
from tests.teatree_agents._read_only_review_world import STAND_IN_BYTES, read_only_context, read_only_review_world

_HEADROOM_BYTES = 4096
_CODEX_WINDOW_TOKENS = 258_400
_MEASURED_BYTES_PER_TOKEN = 3
_PHASES = (
    ("planning", False, "--- SKILL: architecture-design ---"),
    ("reviewing", False, "--- SKILL: review ---"),
    ("reviewing", True, "--- SKILL: review ---"),
    ("critic_reviewing", False, "--- SKILL: review ---"),
    ("answering", False, "--- SKILL: answerer ---"),
    ("requesting_review", False, "--- SKILL: review-request ---"),
    ("shipping", False, "--- SKILL: ship ---"),
    ("backlog_sweep", False, "--- SKILL: sweeping-tickets ---"),
)


class TestReadOnlyPhaseBudget(TestCase):
    def test_every_capped_brief_field_fits_eighty_percent_at_measured_token_density(self) -> None:
        caps = {
            "issue_body": ISSUE_BODY_CAP_BYTES,
            "issue_comments": ISSUE_COMMENTS_CAP_BYTES,
            "ticket_context": TICKET_CONTEXT_CAP_BYTES,
            "handoff": HANDOFF_CAP_BYTES,
            "survey": SURVEY_CAP_BYTES,
            "review_diff": REVIEW_DIFF_CAP_BYTES,
            "review_checks": REVIEW_CHECKS_CAP_BYTES,
            "draft_facts": DRAFT_FACTS_CAP_BYTES,
        }
        assert {field.name for field in fields(ReadOnlyFacts)} == {*caps, "review_head"}
        worst_bytes = MAX_APPEND_BYTES + sum(caps.values()) + 8 * 1024 + 40
        assert 5 * worst_bytes <= 4 * _CODEX_WINDOW_TOKENS * _MEASURED_BYTES_PER_TOKEN

    def test_every_read_only_phase_keeps_headroom_and_required_guidance(self) -> None:
        sizes: dict[str, int] = {}
        with read_only_review_world():
            for phase, reviewer, lifecycle in _PHASES:
                fitted, continued = fit_read_only_context(read_only_context(phase, reviewer=reviewer))
                context = read_only_system_context(fitted)
                label = f"{phase}:{'reviewer' if reviewer else 'author'}"
                sizes[label] = len(context.encode())
                assert context.startswith("\n".join(READ_ONLY_DIRECTIVE)), label
                assert "--- RULES.md ---" in context, label
                assert lifecycle in context + continued, label
                assert "summary" in context, label
                if phase == "planning":
                    assert all("x" * size in context for size in STAND_IN_BYTES.values() if size > 10_000), label
        assert all(size <= MAX_APPEND_BYTES - _HEADROOM_BYTES for size in sizes.values()), sizes
