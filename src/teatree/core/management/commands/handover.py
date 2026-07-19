"""``t3 <overlay> handover`` — hand all current work to another session.

Reuses the PreCompact durable-state snapshot as the hand-off payload and
the ``t3-master`` slot for the default target. ``create`` persists a
:class:`~teatree.core.models.SessionHandover` row (source of truth) and
mirrors it to the XDG file; ``whoami`` prints this session's id;
``claim-on-start`` is the SessionStart-hook entry point that atomically
claims an unclaimed hand-off for a starting session and returns its payload.

ORM access is here (a management command, not a plain typer command) per
the project's "anything touching the ORM is a management command" rule.
"""

import json
from pathlib import Path
from typing import Annotated

import typer
from django_typer.management import TyperCommand, command, initialize

from teatree.core.handover import create_handover
from teatree.core.handover_orchestration import SubagentPush, drive_subagents_to_fast_push
from teatree.loop.session_identity import current_session_id


class Command(TyperCommand):
    help = "Hand all current work from this session to another session."

    @initialize()
    def init(self) -> None:
        """``t3 <overlay> handover`` group root."""

    @command()
    def create(
        self,
        *,
        to: Annotated[
            str,
            typer.Option("--to", help="Target session id. Omit to hand to the live loop owner, else park for next."),
        ] = "",
        drive_subagents: Annotated[
            bool,
            typer.Option(
                "--drive-subagents/--no-drive-subagents",
                help="Fast-push in-flight sub-agent worktrees before they are terminated (directive #8).",
            ),
        ] = True,
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Hand this session's full durable state to another session.

        No ``--to`` → the live ``t3-master`` slot holder; if none, parked
        for whichever session starts next. Always persists the
        :class:`SessionHandover` row AND mirrors it to the XDG file. Then, per
        directive #8, drives every in-flight sub-agent worktree through
        leak-gated fast-push so their work is committed/pushed/PR'd BEFORE the
        orchestrator terminates them.
        """
        from_session = current_session_id()
        if not from_session:
            msg = "no Claude session id — run inside a Claude Code session to hand off its state"
            if json_output:
                self.stdout.write(json.dumps({"ok": False, "error": msg}, indent=2))
            else:
                self.stdout.write(f"ERROR  {msg}")
            raise SystemExit(2)

        handover, mirror = create_handover(from_session=from_session, explicit_to=to)
        recipient = handover.to_session or "next-session"
        pushes = self._drive_subagents() if drive_subagents else []
        if json_output:
            self.stdout.write(
                json.dumps(
                    {
                        "ok": True,
                        "from_session": handover.from_session,
                        "to_session": handover.to_session,
                        "parked_for_next": handover.is_for_next_session,
                        "mirror_path": str(mirror),
                        "subagent_pushes": [self._push_json(push) for push in pushes],
                    },
                    indent=2,
                )
            )
        else:
            self.stdout.write(f"OK    handed off to {recipient}; mirror written to {mirror}.")
            for push in pushes:
                self.stdout.write(f"      sub-agent {push.branch}: {self._push_summary(push)}")

    def _drive_subagents(self) -> list[SubagentPush]:
        """Fast-push in-flight sub-agent worktrees; a failure here never fails the hand-off.

        The hand-off row + mirror are already durable, so the orchestration
        step is best-effort: a git/network hiccup is logged and swallowed
        rather than losing the recorded hand-off.
        """
        cwd = Path.cwd()
        try:
            return drive_subagents_to_fast_push(str(cwd), exclude=(cwd,))
        except Exception:  # noqa: BLE001 — the hand-off is already persisted; sub-agent driving must not fail it
            self.stderr.write(f"WARN  could not drive sub-agents to fast-push from {cwd} (hand-off still recorded).")
            return []

    @staticmethod
    def _push_json(push: SubagentPush) -> dict[str, object]:
        outcome = push.outcome
        return {
            "worktree": str(push.worktree),
            "branch": push.branch,
            "driven": push.driven,
            "committed": bool(outcome and outcome.committed),
            "pushed": bool(outcome and outcome.pushed),
            "pr_url": outcome.pr_url if outcome else "",
            "error": push.error,
        }

    @staticmethod
    def _push_summary(push: SubagentPush) -> str:
        if not push.driven:
            return f"NOT pushed ({push.error or 'unknown error'})"
        outcome = push.outcome
        if outcome is None or not outcome.ok:
            findings = "; ".join(f.detail for f in outcome.findings) if outcome else "no outcome"
            return f"REFUSED ({findings})"
        pr = f" PR {outcome.pr_url}" if outcome.pr_url else ""
        return f"pushed (committed={outcome.committed}){pr}"

    @command()
    def whoami(
        self,
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Print this Claude session's own id (the hand-off ``--to`` target)."""
        session_id = current_session_id()
        if json_output:
            self.stdout.write(json.dumps({"session_id": session_id}, indent=2))
        elif session_id:
            self.stdout.write(session_id)
        else:
            self.stdout.write("(no Claude session id — not running inside a Claude Code session)")

    @command(name="claim-on-start")
    def claim_on_start(
        self,
        *,
        session: Annotated[str, typer.Option("--session", help="The starting session id claiming a hand-off.")] = "",
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = True,
    ) -> None:
        """Atomically claim an unclaimed hand-off for *session* and print its payload.

        The SessionStart hook calls this for a fresh / non-owner session: it
        claims a hand-off targeted AT the session (preferred) or parked for
        "next session", marks it claimed so it injects exactly once, and
        prints the payload. Empty payload when nothing is claimable.
        """
        from teatree.core.models import SessionHandover  # noqa: PLC0415 — deferred: ORM import needs the app registry

        session_id = session or current_session_id()
        claimed = SessionHandover.objects.claim_next(session_id) if session_id else None
        payload = claimed.payload if claimed else ""
        if json_output:
            self.stdout.write(
                json.dumps(
                    {
                        "claimed": claimed is not None,
                        "from_session": claimed.from_session if claimed else "",
                        "payload": payload,
                    },
                    indent=2,
                )
            )
        elif payload:
            self.stdout.write(payload)
