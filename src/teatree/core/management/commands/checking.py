"""``t3 <overlay> checking show`` — terse, read-only "what did I miss" report (#1529).

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

Read-only: every query underneath is a select; the command never transitions a
ticket nor writes any row except the checkpoint marker. The return value is the
output channel (``django-typer`` serialises it) — JSON when ``--json``, else
the terse human view.
"""

import hashlib
import io
import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

import typer
from django.utils import timezone
from django_typer.management import TyperCommand, command, initialize

from teatree.backends.slack.table_format import render_table_message
from teatree.core.checking import DEFAULT_CAP, CheckGroup, gather_all_overlays_report, gather_checking_report
from teatree.core.checkpoint import advance_checkpoint_monotonic, checkpoint_path, resolve_window_start
from teatree.core.notify import NotifyKind, notify_user
from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.ref_render import render_ref
from teatree.core.table_output import print_table
from teatree.types import RawAPIDict


@dataclass(frozen=True, slots=True)
class _ShowFlags:
    """The ``checking show`` window/output/side-effect flags, bundled to keep the dispatch narrow."""

    since: str
    json_output: bool
    no_advance: bool
    notify: bool


def _stamp(when: datetime) -> str:
    local = timezone.localtime(when) if timezone.is_aware(when) else when
    return local.strftime("%H:%M")


def _render_groups_tables(groups: list[CheckGroup], *, header: str, stamp: str, cap: int = DEFAULT_CAP) -> str:
    """Render each non-empty group as a table under *header*.

    Empty report (every group at ``total == 0``) collapses to the single
    ``Nothing since <stamp>.`` line the terse view uses, so a no-change run
    stays one line. A capped group notes the pre-cap total in its title.
    """
    if all(group.total == 0 for group in groups):
        return f"Nothing since {stamp}."
    buffer = io.StringIO()
    buffer.write(header + "\n")
    for group in groups:
        if group.total == 0:
            continue
        title = group.title if group.total <= cap else f"{group.title} — {group.total} (top {cap})"
        print_table(
            ["Ref", "Detail"],
            [[render_ref(item.label, title=item.title, url=item.url), item.detail] for item in group.items[:cap]],
            title=title,
            stream=buffer,
        )
    return buffer.getvalue()


def _recap_table_dm(groups: list[CheckGroup], *, header: str, cap: int = DEFAULT_CAP) -> tuple[list[RawAPIDict], str]:
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
        rows = [[item.label, item.title, item.detail] for item in group.items[:cap]]
        message = render_table_message(["Ref", "Title", "Detail"], rows, title=f"{group.title} ({group.total})")
        blocks.extend(message.blocks)
        fences.append(message.fence)
    if not blocks:
        return [], ""
    return blocks, f"{header}\n" + "\n".join(fences)


def _recap_key(groups: list[CheckGroup], *, scope: str) -> str:
    """Content-hashed idempotency key so an unchanged recap never re-DMs."""
    payload = "\n".join(f"{group.title}|{item.label}|{item.detail}" for group in groups for item in group.items)
    digest = hashlib.sha256(payload.encode()).hexdigest()[:16]
    return f"checking_recap:{scope}:{digest}"


def _maybe_notify_recap(groups: list[CheckGroup], *, header: str, scope: str, notify: bool) -> None:
    """DM the recap through the real bot→user egress when ``--notify`` is set (#2966)."""
    if not notify:
        return
    blocks, fence = _recap_table_dm(groups, header=header)
    if not blocks:
        return
    notify_user(
        fence,
        kind=NotifyKind.INFO,
        idempotency_key=_recap_key(groups, scope=scope),
        audience=NotifyAudience.OWNER_DELIVERY,
        blocks=blocks,
    )


class Command(TyperCommand):
    @initialize()
    def init(self) -> None:
        """``t3 <overlay> checking`` group root."""

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
    ) -> str:
        """Print a terse, grouped, clickable report of changes since the last check."""
        overlay_name = os.environ.get("T3_OVERLAY_NAME", "")
        now = timezone.now()

        _validate_since(since)

        flags = _ShowFlags(since=since, json_output=json_output, no_advance=no_advance, notify=notify)
        if this_overlay:
            return self._show_single_overlay(overlay_name=overlay_name, now=now, flags=flags)
        return self._show_all_overlays(now=now, flags=flags)

    def _show_single_overlay(self, *, overlay_name: str, now: datetime, flags: _ShowFlags) -> str:
        """Single-overlay path (``--this-overlay`` or backward-compat)."""
        window_start = resolve_window_start(since=flags.since, now=now)
        report = gather_checking_report(
            since=window_start,
            now=now,
            overlay_name=overlay_name,
            code_host=self._resolve_code_host(),
            overlay_repos=self._resolve_overlay_repos(),
        )
        if not flags.since and not flags.no_advance:
            advance_checkpoint_monotonic(now)
        stamp = _stamp(report.since)
        header = f"Since {stamp} · {overlay_name}" if overlay_name else f"Since {stamp}"
        groups = [report.merged, report.in_flight, report.needs_you]
        _maybe_notify_recap(groups, header=header, scope=overlay_name or "global", notify=flags.notify)
        if flags.json_output:
            return json.dumps(report.to_dict())
        return _render_groups_tables(groups, header=header, stamp=stamp)

    def _show_all_overlays(self, *, now: datetime, flags: _ShowFlags) -> str:
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

        if not flags.since and not flags.no_advance:
            for name in overlays:
                path = checkpoint_path(overlay=name)
                advance_checkpoint_monotonic(now, path)

        stamp = _stamp(report.earliest_since)
        header = f"Since {stamp} · all overlays"
        groups = [report.merged, report.in_flight, report.needs_you]
        _maybe_notify_recap(groups, header=header, scope="all", notify=flags.notify)
        if flags.json_output:
            return json.dumps(report.to_dict())
        return _render_groups_tables(groups, header=header, stamp=stamp)

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


def _overlay_code_host(overlay: object) -> str:
    try:
        return overlay.config.code_host or ""  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 — best-effort render; a failure degrades to empty
        return ""


def _overlay_repos(overlay: object) -> list[str]:
    try:
        repos = list(overlay.metadata.get_followup_repos()) + list(overlay.get_repos())  # type: ignore[union-attr]
        return [r for r in repos if isinstance(r, str) and r]
    except Exception:  # noqa: BLE001 — best-effort; a failure degrades to no rows
        return []
