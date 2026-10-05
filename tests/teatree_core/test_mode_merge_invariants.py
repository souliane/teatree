"""The non-negotiable invariants the availability+preset merge must not regress (#61).

Locks the five owner-flagged invariants around the merged :class:`Mode` (Mode):

1.  Owner-reply ALWAYS-ON — the reactive owner-DM reply recorder never consults the
    merged-mode resolver / defer predicate (static guard; the functional proof lives
    in ``tests/teatree_agents/test_owner_answer_threading.py``).
2.  Auto-merge under away — loop membership, proven in
    ``tests/teatree_loops/test_loop_table.py::TestAutoMergePathAdmittedUnderAway``.
3.  Live-presence #189 escape — the transcript's recent owner action still gates a
    per-turn in-client render, independent of the named mode (``tests/test_owner_prompts.py``).
4.  autoload gate — untouched by the merge (no mode read added to it).
5.  ``require_human_approval_to_merge`` stays a SEPARATE knob — it is NOT folded into
    the merged Mode (design decision D).
"""

from pathlib import Path

import django.test

from teatree.core.models import Mode


class TestOwnerReplyAlwaysOn(django.test.SimpleTestCase):
    """Invariant 1: the owner-reply recorder must never grow a mode/defer gate."""

    _FORBIDDEN = ("resolve_active_mode", "mode_resolution", "resolve_mode", "defers_questions")

    def test_recorder_source_never_reads_the_mode(self) -> None:
        import teatree.agents.reactive_envelope_recorders as recorders  # noqa: PLC0415 — test-time module inspection

        # This lane reads the recorder's SOURCE, so a module with no file on disk leaves it
        # asserting over nothing — say that, rather than letting `None` reach `Path()`.
        assert recorders.__file__ is not None, "the recorder has no source file to inspect"
        source = Path(recorders.__file__).read_text(encoding="utf-8")
        offenders = [token for token in self._FORBIDDEN if token in source]
        assert offenders == [], f"owner-reply recorder must stay mode-independent — found: {offenders}"


class TestRequireHumanApprovalStaysSeparate(django.test.SimpleTestCase):
    """Invariant 5: the merge-approval knob is NOT an attribute of the merged Mode."""

    def test_mode_has_no_merge_approval_field(self) -> None:
        field_names = {field.name for field in Mode._meta.get_fields()}
        assert "require_human_approval_to_merge" not in field_names
        # The merged Mode carries the loop mask and the egress posture read at selection
        # time. `overlay_scope` left it with #4202's follow-up — scanner scope is not a
        # property of the preset.
        assert {"entries", "egress"} <= field_names
        assert "overlay_scope" not in field_names
        assert not any("approval" in name or "merge" in name for name in field_names)
