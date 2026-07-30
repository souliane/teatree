"""``worktree diagram`` renders through the one canonical FSM renderer.

There were two FSM-diagram renderers: the canonical
``teatree.core.diagrams.render_fsm_mermaid`` (drift-gated, byte-stable, reused by
the generate/check hooks) and a private ``_fsm_diagram`` copy inside the
``worktree`` command. Two renderers drift; the command must call the canonical
one so its output can never diverge from the committed diagrams.
"""

from typing import cast

from django.core.management import call_command
from django.test import TestCase
from django.test.utils import override_settings

from teatree.core.diagrams import render_fsm_mermaid
from teatree.core.management.commands import worktree as worktree_cmd
from teatree.core.models import Ticket, Worktree
from tests.teatree_core.management_commands.test_lifecycle import FULL_OVERLAY, SETTINGS, _patch_overlays


class TestDiagramCanonicalRenderer(TestCase):
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_worktree_output_equals_render_fsm_mermaid(self) -> None:
        result = cast("str", call_command("worktree", "diagram"))
        assert result == render_fsm_mermaid(Worktree)

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_ticket_output_equals_render_fsm_mermaid(self) -> None:
        result = cast("str", call_command("worktree", "diagram", model="ticket"))
        assert result == render_fsm_mermaid(Ticket)

    def test_no_local_fsm_diagram_renderer_remains(self) -> None:
        assert not hasattr(worktree_cmd, "_fsm_diagram"), (
            "the private _fsm_diagram copy must be deleted — the diagram command uses the "
            "canonical teatree.core.diagrams.render_fsm_mermaid so there is exactly one renderer."
        )
