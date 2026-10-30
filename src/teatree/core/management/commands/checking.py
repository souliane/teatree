"""``t3 <overlay> checking show`` — terse "what did I miss" report (#1529).

Thin wrapper over :func:`teatree.core.checking.gather_checking_report`. The
command reads the prior checkpoint, gathers the window ``[window_start, now)``,
and advances the marker to ``now`` *after* gathering — so an immediate second
run reports an empty window rather than the first run collapsing its own
window. The marker advances ONLY on the default path: ``--since`` (the user
named an explicit window) and ``--no-advance`` (an inspection-only run) both
leave the checkpoint untouched.

Default path (no ``--this-overlay``): aggregates all configured overlays,
advancing each overlay's marker independently after gathering. ``--this-overlay``
restores the pre-existing single-overlay scope (backward-compat).

Before gathering, ``show`` and ``brief`` reconcile owner questions with recorded
answers and may resume parked tasks. Only the default path writes a checkpoint
marker. ``shoot`` posts Slack questions and records delivery state.
The report is routed through the machine-output seam — JSON on stdout under
``--json``, the terse human view on stderr — and returned as the typed payload.
"""

import hashlib
import io
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import IO, TYPE_CHECKING, Annotated, cast

import typer
from django.utils import timezone
from django_typer.management import command, initialize

from teatree.backends.slack.listener_heartbeat import ListenerBeat, heartbeat_is_stale, read_heartbeat
from teatree.backends.slack.table_format import render_table_message, slack_table_block
from teatree.core.checking import (
    DEFAULT_CAP,
    AllOverlaysReportDict,
    CheckGroup,
    CheckingReportDict,
    gather_all_overlays_report,
    gather_checking_report,
)
from teatree.core.checking_report.slack import ShootReportDict, shoot_questions
from teatree.core.checking_report.status import plain_title
from teatree.core.checking_types import CheckItem, OwnerWaitItem
from teatree.core.checkpoint import advance_checkpoint_monotonic, checkpoint_path, resolve_window_start
from teatree.core.machine_output import MachineOutputCommand, emit
from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.notify import NotifyKind, notify_user
from teatree.core.question_heal import live_owner_questions
from teatree.core.ref_render import render_ref
from teatree.core.table_output import print_table
from teatree.types import RawAPIDict

if TYPE_CHECKING:
    from teatree.core.overlay import OverlayBase


@dataclass(frozen=True, slots=True)
class _ShowFlags:
    """The ``checking show`` window/output/side-effect flags, bundled to keep the dispatch narrow."""

    since: str
    json_output: bool
    no_advance: bool
    notify: bool
    brief: bool


@dataclass(frozen=True, slots=True)
class _RenderContext:
    header: str
    stamp: str
    status_lines: list[str]


@dataclass(frozen=True, slots=True)
class _GatheredReport:
    payload: CheckingReportDict | AllOverlaysReportDict
    groups: list[CheckGroup]
    context: _RenderContext


def _stamp(when: datetime) -> str:
    local = timezone.localtime(when) if timezone.is_aware(when) else when
    return local.strftime("%H:%M")


def _listener_stale(beat: ListenerBeat | None, *, now: float) -> bool:
    if beat is None:
        return True
    if beat.stamps is not None:
        return all(heartbeat_is_stale(beat, name, now=now) for name in beat.stamps)
    return heartbeat_is_stale(beat, "", now=now)


def _brief_critical_lines(groups: list[CheckGroup]) -> list[str]:
    lines: list[str] = []
    for group in groups:
        if group.title == "Remaining":
            lines.extend(
                f"Held pull request: [{plain_title(item.title)}]({item.url})" for item in group.critical_items[:2]
            )
    return lines


def _render_brief(groups: list[CheckGroup], *, context: _RenderContext, internal_escalations: int) -> str:
    status = [*context.status_lines, *_brief_critical_lines(groups)]
    if status:
        if internal_escalations:
            if context.status_lines:
                status[0] = f"{status[0]} · {internal_escalations} internal escalations"
            else:
                status.append(f"{internal_escalations} internal escalations")
        return "\n".join(status)
    if all(not group.has_table_content() for group in groups) and not internal_escalations:
        return f"Nothing since {context.stamp}."
    suffix = f" · {internal_escalations} internal escalations" if internal_escalations else ""
    return f"No active feature work since {context.stamp}{suffix}."


def _render_groups_tables(
    groups: list[CheckGroup],
    *,
    context: _RenderContext,
    internal_escalations: int = 0,
    brief: bool = False,
) -> str:
    """Render each non-empty group as a table under *header*.

    Empty report (every group has no table content) collapses to the single
    ``Nothing since <stamp>.`` line the terse view uses, so a no-change run
    stays one line. A capped group notes the pre-cap total in its title.
    """
    if brief:
        return _render_brief(groups, context=context, internal_escalations=internal_escalations)
    status = context.status_lines
    cap = DEFAULT_CAP
    if all(not group.has_table_content() for group in groups) and not internal_escalations and not status:
        return f"Nothing since {context.stamp}."
    buffer = io.StringIO()
    if status:
        buffer.write("\n".join(status) + "\n")
    buffer.write(context.header + "\n")
    for group in groups:
        if not group.has_table_content():
            continue
        title = group.title if group.total <= cap else f"{group.title} — {group.total} (top {cap})"
        rows = [
            [render_ref(item.label, title=item.title, url=item.url), item.detail] for item in group.table_items(cap=cap)
        ]
        if group.total > cap:
            rows.append(["", f"…and {group.total - cap} more"])
        print_table(
            ["Ref", "Detail"],
            rows,
            title=title,
            stream=buffer,
        )
    if internal_escalations:
        buffer.write(f"{internal_escalations} internal escalations: factory work, not questions\n")
    return buffer.getvalue()


def _recap_table_dm(
    groups: list[CheckGroup], *, header: str, internal_escalations: int = 0, cap: int = DEFAULT_CAP
) -> tuple[list[RawAPIDict], str]:
    """Render the non-empty recap groups as native ``table`` blocks + a fence.

    Each group becomes a titled :func:`render_table_message` — the native
    Block Kit ``table`` block for a rich rendering plus the monospace fence for
    the ``text`` degradation path. Returns ``([], "")`` when every group is
    empty so the caller skips the DM (an empty recap never pings).
    """
    blocks: list[RawAPIDict] = []
    fences: list[str] = []
    for group in groups:
        if group.total == 0:
            continue
        rows = [
            [
                f"Q{item.row_id}" if isinstance(item, OwnerWaitItem) and item.row_id is not None else item.label,
                item.title or item.url or item.label.partition(": ")[2] or item.label,
                item.detail,
            ]
            for item in group.items[:cap]
        ]
        links = {(index, 1): item.url for index, item in enumerate(group.items[:cap]) if item.url}
        headers = ["Ref", "Subject", "Detail"]
        message = render_table_message(headers, rows, title=f"{group.title} ({group.total})")
        message.blocks[-1] = slack_table_block(headers, rows, links=links)
        blocks.extend(message.blocks)
        fences.append(message.fence)
    if internal_escalations:
        line = f"{internal_escalations} internal escalations: factory work, not questions"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": line}})
        fences.append(line)
    if not fences:
        return [], ""
    return blocks, f"{header}\n" + "\n".join(fences)


def _item_identity(item: CheckItem) -> str:
    detail = f"{item.kind}:{item.row_id}:{item.owner_only}" if isinstance(item, OwnerWaitItem) else item.detail
    return f"{item.label}|{item.url}|{item.title}|{detail}"


def _recap_key(groups: list[CheckGroup], *, scope: str, internal_escalations: int = 0) -> str:
    """Content-hashed idempotency key so an unchanged recap never re-DMs."""
    payload = "\n".join(f"{group.title}|{_item_identity(item)}" for group in groups for item in group.items)
    payload += f"\ninternal_escalations:{internal_escalations}"
    digest = hashlib.sha256(payload.encode()).hexdigest()[:16]
    return f"checking_recap:{scope}:{digest}"


def _maybe_notify_recap(
    groups: list[CheckGroup], *, header: str, scope: str, notify: bool, internal_escalations: int = 0
) -> bool:
    """Whether the recap DM landed — trivially true when there is nothing to send (#2966)."""
    if not notify:
        return True
    blocks, fence = _recap_table_dm(groups, header=header, internal_escalations=internal_escalations)
    if not fence:
        return True
    return notify_user(
        fence,
        kind=NotifyKind.INFO,
        idempotency_key=_recap_key(groups, scope=scope, internal_escalations=internal_escalations),
        audience=NotifyAudience.OWNER_DELIVERY,
        blocks=blocks,
    )


class Command(MachineOutputCommand):
    @initialize()
    def init(self) -> None:
        """``t3 <overlay> checking`` group root."""

    @command()
    def shoot(
        self,
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit the Slack question links as JSON.")] = False,
    ) -> ShootReportDict:
        """Post or nudge waiting owner questions through the existing Slack drains."""
        overlay = os.environ.get("T3_OVERLAY_NAME", "")
        stale = _listener_stale(read_heartbeat(), now=time.time())
        result = shoot_questions(overlay=overlay, listener_stale=stale)
        self.print_result = False
        human = "\n".join(f"[{card['label']}]({card['permalink']})" for card in result["cards"])
        if result["reason"]:
            human = f"{result['reason']}\n{human}"
        if result["remaining"]:
            human += f"\n{result['remaining']} waiting for the next Slack send"
        emit(
            result,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=human.strip(),
        )
        return result

    @command()
    def show(
        self,
        *,
        since: Annotated[
            str,
            typer.Option(help="ISO timestamp override for the window start (does NOT advance the marker)."),
        ] = "",
        json_output: Annotated[
            bool,
            typer.Option("--json", help="Emit the structured report as JSON instead of the terse view."),
        ] = False,
        no_advance: Annotated[
            bool,
            typer.Option("--no-advance", help="Read the window without advancing the last-checked marker."),
        ] = False,
        this_overlay: Annotated[
            bool,
            typer.Option(
                "--this-overlay",
                help="Scope to the current overlay only (default: aggregate all configured overlays).",
            ),
        ] = False,
        notify: Annotated[
            bool,
            typer.Option(
                "--notify",
                help="Also DM the recap to you as a Slack table (native Block Kit + monospace fence fallback).",
            ),
        ] = False,
    ) -> CheckingReportDict | AllOverlaysReportDict:
        """Reconcile owner questions and print a terse, grouped, clickable report of changes since the last check."""
        flags = _ShowFlags(since=since, json_output=json_output, no_advance=no_advance, notify=notify, brief=False)
        return self._dispatch_show(flags=flags, this_overlay=this_overlay)

    @command()
    def brief(
        self,
        *,
        since: Annotated[str, typer.Option(help="ISO timestamp override for the window start.")] = "",
        no_advance: Annotated[bool, typer.Option("--no-advance", help="Do not advance the checkpoint.")] = False,
        this_overlay: Annotated[bool, typer.Option("--this-overlay", help="Use the current overlay only.")] = False,
    ) -> CheckingReportDict | AllOverlaysReportDict:
        """Print the one-screen status while keeping full groups in the returned payload."""
        flags = _ShowFlags(since=since, json_output=False, no_advance=no_advance, notify=False, brief=True)
        return self._dispatch_show(flags=flags, this_overlay=this_overlay)

    def _dispatch_show(self, *, flags: _ShowFlags, this_overlay: bool) -> CheckingReportDict | AllOverlaysReportDict:
        overlay_name = os.environ.get("T3_OVERLAY_NAME", "")
        now = timezone.now()
        _validate_since(flags.since)
        live_owner_questions()

        gathered = (
            self._show_single_overlay(overlay_name=overlay_name, now=now, flags=flags)
            if this_overlay
            else self._show_all_overlays(now=now, flags=flags)
        )
        self.print_result = False
        emit(
            gathered.payload,
            json_output=flags.json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=_render_groups_tables(
                gathered.groups,
                context=gathered.context,
                internal_escalations=gathered.payload["internal_escalations"],
                brief=flags.brief,
            ),
        )
        return gathered.payload

    def _show_single_overlay(self, *, overlay_name: str, now: datetime, flags: _ShowFlags) -> _GatheredReport:
        """Single-overlay path (``--this-overlay`` or backward-compat)."""
        window_start = resolve_window_start(since=flags.since, now=now)
        report = gather_checking_report(
            since=window_start,
            now=now,
            overlay_name=overlay_name,
            code_host=self._resolve_code_host(),
            overlay_repos=self._resolve_overlay_repos(),
        )
        stamp = _stamp(report.since)
        header = f"Since {stamp} · {overlay_name}" if overlay_name else f"Since {stamp}"
        groups = report.groups
        # The checkpoint consumes the window, so advancing it over an undelivered
        # recap loses those rows for good — deliver first, advance only on success.
        delivered = _maybe_notify_recap(
            report.recap_groups,
            header=header,
            scope=overlay_name or "global",
            notify=flags.notify,
            internal_escalations=report.internal_escalations,
        )
        if not delivered:
            self.stderr.write("  recap DM not delivered — the checking window is left unadvanced")
        if not flags.since and not flags.no_advance and delivered:
            advance_checkpoint_monotonic(now)
        context = _RenderContext(header=header, stamp=stamp, status_lines=report.status_by_feature.render())
        return _GatheredReport(report.to_dict(), groups, context)

    def _show_all_overlays(self, *, now: datetime, flags: _ShowFlags) -> _GatheredReport:
        """All-overlays path (default): aggregate every configured overlay."""
        from teatree.core.overlay_loader import get_all_overlays  # noqa: PLC0415 — deferred: keeps command import light

        overlays = get_all_overlays()

        overlay_windows: dict[str, tuple[datetime, datetime]] = {}
        overlay_configs: dict[str, tuple[str, list[str]]] = {}

        for name, overlay in overlays.items():
            path = checkpoint_path(overlay=name)
            window_start = resolve_window_start(since=flags.since, now=now, path=path)
            overlay_windows[name] = (window_start, now)
            code_host = _overlay_code_host(overlay)
            repos = _overlay_repos(overlay)
            overlay_configs[name] = (code_host, repos)

        report = gather_all_overlays_report(
            overlay_windows=overlay_windows,
            overlay_configs=overlay_configs,
        )

        stamp = _stamp(report.earliest_since)
        header = f"Since {stamp} · all overlays"
        groups = report.groups
        delivered = _maybe_notify_recap(
            report.recap_groups,
            header=header,
            scope="all",
            notify=flags.notify,
            internal_escalations=report.internal_escalations,
        )
        if not delivered:
            self.stderr.write("  recap DM not delivered — the checking window is left unadvanced")
        if not flags.since and not flags.no_advance and delivered:
            for name in overlays:
                path = checkpoint_path(overlay=name)
                advance_checkpoint_monotonic(now, path)

        context = _RenderContext(header=header, stamp=stamp, status_lines=report.status_by_feature.render())
        return _GatheredReport(report.to_dict(), groups, context)

    @staticmethod
    def _resolve_code_host() -> str:
        """Resolve the overlay's ``code_host`` string for the URL builder (no forge call).

        Reads ``overlay.config.code_host`` directly — a pure config read, never
        a network call. A missing or unloadable overlay degrades to an empty
        host (the builder then defaults to the GitHub URL shape).
        """
        try:
            from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415 — deferred: keeps command import light

            return get_overlay().config.code_host or ""
        except Exception:  # noqa: BLE001 — config read must never wedge a read-only report
            return ""

    @staticmethod
    def _resolve_overlay_repos() -> list[str]:
        """Resolve the overlay's repo identifiers used to scope NULL-ticket merges (#1559).

        Unions ``get_followup_repos()`` (``owner/repo``) with ``get_repos()``
        (often a bare ``repo`` name) so a ceremony CLEAR whose resolved repo
        matches either shape is scoped to this overlay. A missing or unloadable
        overlay (or a hook that raises) degrades to an empty list — the merged
        group then keeps the ticket-bearing back-compat scope only.
        """
        try:
            from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415 — deferred: keeps command import light

            overlay = get_overlay()
            repos = list(overlay.metadata.get_followup_repos()) + list(overlay.get_repos())
            return [repo for repo in repos if isinstance(repo, str) and repo]
        except Exception:  # noqa: BLE001 — config read must never wedge a read-only report
            return []


def _validate_since(since: str) -> None:
    """Reject a malformed ``--since`` at the command boundary (#1652).

    ``resolve_window_start`` raises ``ValueError`` on an unparsable
    timestamp; surface it as a ``typer.BadParameter`` so the user sees a
    clean "expected ISO-8601" message and a non-zero exit rather than a raw
    traceback (mirrors ``availability._parse_until``).
    """
    if not since.strip():
        return
    try:
        resolve_window_start(since=since, now=timezone.now())
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _overlay_code_host(overlay: "OverlayBase") -> str:
    try:
        return overlay.config.code_host or ""
    except Exception:  # noqa: BLE001 — best-effort render; a failure degrades to empty
        return ""


def _overlay_repos(overlay: "OverlayBase") -> list[str]:
    try:
        repos = list(overlay.metadata.get_followup_repos()) + list(overlay.get_repos())
        return [r for r in repos if isinstance(r, str) and r]
    except Exception:  # noqa: BLE001 — best-effort; a failure degrades to no rows
        return []
