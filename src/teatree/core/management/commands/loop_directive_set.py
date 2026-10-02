"""``manage.py loop_directive_set`` — switch a standing directive off and back on (#4166).

Backs ``t3 loop directives disable|enable``, the write half of the standing-directive
surface. The mechanism it drives already existed and was already tested — an override
``Prompt`` whose body strips to empty switches that slot off — but no surface could
write one, so the off switch was reachable from nowhere. This is that surface, and it
is the whole-feature kill too: ``--all`` disables every slot.

The disable is VERSIONED and reversible because it goes through
:meth:`~teatree.core.models.Prompt.revise`, which snapshots the superseded body as a
``PromptVersion`` before writing the empty one. So ``enable`` restores what the owner
actually had, not merely the compiled default, and the edit history says who turned
what off and when.

Non-zero exits use ``raise SystemExit(N)`` — this runs under Django's
``call_command``, where a ``typer.Exit`` is swallowed and the process reports success
on a real failure.
"""

from typing import Annotated

import typer
from django_typer.management import TyperCommand, command

from teatree.core.models import Prompt
from teatree.loop.standing_directives import STANDING_DIRECTIVES, override_prompt_name

_SlotArgument = Annotated[
    list[str] | None,
    typer.Argument(help="Slot ids to act on; omit with --all for every slot."),
]
_AllOption = Annotated[bool, typer.Option("--all", help="Act on every standing-directive slot.")]

_KNOWN_SLOT_IDS = tuple(directive.slot_id for directive in STANDING_DIRECTIVES)


class Command(TyperCommand):
    help = "Switch standing-directive slots off (disable) or back on (enable) (#4166)."

    def _resolve_slots(self, slot_ids: list[str] | None, *, every: bool) -> list[str]:
        """The slots to act on, or exit non-zero naming the valid ids."""
        requested = list(slot_ids or [])
        # Validated BEFORE --all is honoured: an unknown id alongside --all is a
        # typo the owner is owed, not a licence to act on every slot instead.
        unknown = [slot for slot in requested if slot not in _KNOWN_SLOT_IDS]
        if unknown:
            self.stderr.write(f"  unknown slot(s) {', '.join(unknown)}. Valid: {', '.join(_KNOWN_SLOT_IDS)}")
            raise SystemExit(2)
        if every:
            return list(_KNOWN_SLOT_IDS)
        if not requested:
            self.stderr.write(f"  name at least one slot, or pass --all. Valid: {', '.join(_KNOWN_SLOT_IDS)}")
            raise SystemExit(2)
        return requested

    @command()
    def disable(self, slot_ids: _SlotArgument = None, *, all_slots: _AllOption = False) -> None:
        """Switch each named slot off by writing an empty override body.

        Idempotent: a slot already off is left exactly as it is, so re-running this
        churns no version history.
        """
        for slot_id in self._resolve_slots(slot_ids, every=all_slots):
            prompt, created = Prompt.objects.get_or_create(
                name=override_prompt_name(slot_id),
                defaults={"body": "", "description": f"Standing directive override for {slot_id}."},
            )
            if not created:
                prompt.revise(body="")
            self.stderr.write(f"  {slot_id}: off")

    @command()
    def enable(self, slot_ids: _SlotArgument = None, *, all_slots: _AllOption = False) -> None:
        """Switch each named slot back on, restoring the owner's own text where there is one.

        With a snapshotted body, that body comes back; with none, the override row is
        removed so the compiled default resolves again.

        A slot that is ALREADY on is left exactly as it is. ``PromptVersion`` rows hold
        SUPERSEDED bodies, never the live one, so restoring the newest non-empty version
        over a live body reverts the owner's latest edit — and with only the disable's
        empty snapshot behind it, the delete branch destroys that edit outright. Both are
        reachable from the documented ``enable --all`` undo of ``disable --all``.
        """
        for slot_id in self._resolve_slots(slot_ids, every=all_slots):
            prompt = Prompt.objects.by_name(override_prompt_name(slot_id))
            if prompt is None:
                self.stderr.write(f"  {slot_id}: on (compiled default)")
                continue
            if prompt.body.strip():
                self.stderr.write(f"  {slot_id}: on (owner text, unchanged)")
                continue
            restored = next((v for v in prompt.versions.order_by("-version") if v.body.strip()), None)
            if restored is None:
                prompt.delete()
                self.stderr.write(f"  {slot_id}: on (compiled default)")
                continue
            prompt.revise(body=restored.body)
            self.stderr.write(f"  {slot_id}: on (restored v{restored.version})")
