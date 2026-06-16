"""Tests for teatree.loop.rendering — line builder under NO_COLOR (#721).

The statusline module documents NO_COLOR (https://no-color.org/) support,
but ``rendering._link`` baked OSC 8 hyperlink escapes into the line text
*before* ``render()`` could honour ``colorize=False`` — so a NO_COLOR
consumer (or anything parsing the file as plain text) got escape-byte
garbage and no ``text <url>`` fallback. These drive the full
``zones_for`` → ``_link`` → ``render`` pipeline under NO_COLOR.
"""

from pathlib import Path

import pytest

from teatree.loop.dispatch import DispatchAction, dispatch
from teatree.loop.rendering import zones_for
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.statusline import render


def _render_blob(actions: list[DispatchAction]) -> str:
    zones = zones_for(actions, colorize=False)
    return "".join(
        item if isinstance(item, str) else item.text
        for zone in (zones.anchors, zones.action_needed, zones.in_flight)
        for item in zone
    )


_ACTIONS = [
    DispatchAction(
        kind="statusline",
        zone="action_needed",
        detail="Ticket 55 — issue_closed",
        payload={
            "reason": "issue_closed",
            "overlay": "teatree",
            "url": "https://example.com/issues/55",
        },
    ),
]


class TestNoColorPipeline:
    def test_zones_for_colorize_false_emits_no_escapes(self) -> None:
        zones = zones_for(_ACTIONS, colorize=False)
        blob = "".join(
            item if isinstance(item, str) else item.text
            for zone in (zones.anchors, zones.action_needed, zones.in_flight)
            for item in zone
        )
        assert "\033" not in blob, repr(blob)
        # The URL must still be present, as a plain `text <url>` fallback.
        assert "https://example.com/issues/55" in blob

    def test_zones_for_colorize_true_keeps_osc8(self) -> None:
        zones = zones_for(_ACTIONS, colorize=True)
        blob = "".join(
            item if isinstance(item, str) else item.text
            for zone in (zones.anchors, zones.action_needed, zones.in_flight)
            for item in zone
        )
        assert "\033]8;;" in blob

    def test_full_render_under_no_color_has_zero_escape_bytes(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("NO_COLOR", "1")
        target = tmp_path / "statusline.txt"
        zones = zones_for(_ACTIONS, colorize=False)
        render(zones, target=target, colorize=False)
        content = target.read_text(encoding="utf-8")
        assert "\033" not in content, repr(content)
        assert "https://example.com/issues/55" in content

    def test_zones_for_defaults_to_env_when_colorize_none(self, monkeypatch) -> None:
        monkeypatch.setenv("NO_COLOR", "1")
        zones = zones_for(_ACTIONS)  # colorize unset -> resolve from env
        blob = "".join(
            item if isinstance(item, str) else item.text
            for zone in (zones.anchors, zones.action_needed, zones.in_flight)
            for item in zone
        )
        assert "\033" not in blob, repr(blob)

    def test_zones_for_default_colorizes_without_no_color(self, monkeypatch) -> None:
        monkeypatch.delenv("NO_COLOR", raising=False)
        zones = zones_for(_ACTIONS)
        blob = "".join(
            item if isinstance(item, str) else item.text
            for zone in (zones.anchors, zones.action_needed, zones.in_flight)
            for item in zone
        )
        assert "\033]8;;" in blob


def _statusline_action(*, detail: str, url: str) -> DispatchAction:
    """Build a reviewer-pr dual-dispatch statusline action.

    Mirrors what ``dispatch._dispatch_one`` produces: ``detail`` is the
    scanner summary and the payload carries ``url``/``overlay`` but no
    ``iid`` (reviewer scanners never ship an iid — see
    ``scanners/reviewer_prs.py``).
    """
    return DispatchAction(
        kind="statusline",
        zone="action_needed",
        detail=detail,
        payload={"url": url, "overlay": "teatree"},
    )


class TestReviewerPrRefRendering:
    """Reviewer-pr signals render as a clickable per-overlay ``!N`` ref.

    They carry only ``url`` (no ``iid``); the renderer derives the iid
    from the URL tail. ``approval_dismissed`` uses a different summary
    ("Approval dismissed:") than new_sha/unreviewed ("Review needed:"),
    so the derivation must cover both prefixes.
    """

    _PR_URL = "https://gitlab.example.com/g/p/-/merge_requests/123"

    def test_review_needed_renders_clickable_pr_ref(self) -> None:
        action = _statusline_action(detail=f"Review needed: {self._PR_URL}", url=self._PR_URL)
        zones = zones_for([action], colorize=False)
        action_blob = "".join(item if isinstance(item, str) else item.text for item in zones.action_needed)
        assert "[teatree] !123" in action_blob, repr(action_blob)

    def test_approval_dismissed_renders_clickable_pr_ref(self) -> None:
        zones = zones_for(
            [_statusline_action(detail=f"Approval dismissed: {self._PR_URL}", url=self._PR_URL)],
            colorize=False,
        )
        action_blob = "".join(item if isinstance(item, str) else item.text for item in zones.action_needed)
        # Before the fix this collapsed into a generic "Approval dismissed:
        # <url>" line with no per-overlay `!N` grouping.
        assert "[teatree] !123" in action_blob, repr(action_blob)


def _disposition_action(
    *,
    reason: str,
    payload_extra: dict[str, object],
    ticket_number: str = "77",
) -> DispatchAction:
    """Mirror what ``dispatch._dispatch_one`` builds for ``unassigned``/etc.

    The disposition scanner ships ``reason`` plus the ticket coordinates;
    ``payload_extra`` adds the reason-specific fields (``old_owner`` /
    ``new_owners`` for ``unassigned``). ``ticket_number`` lets a test vary
    the ticket so dedup-by-ticket behaviour can be exercised.
    """
    return DispatchAction(
        kind="statusline",
        zone="action_needed",
        detail=f"Ticket {ticket_number} — {reason}",
        payload={
            "reason": reason,
            "overlay": "teatree",
            "ticket_number": ticket_number,
            "issue_url": f"https://example.com/issues/{ticket_number}",
            **payload_extra,
        },
    )


class TestReassignedShowsFromTo:
    """Ask 1 — ``reassigned`` must spell out the ownership transition.

    A bare ``reassigned: #77`` told the user nothing. The line now reads
    ``reassigned (from <old> → to <new>): #77`` using the owner identities
    the disposition scanner already had at detection time.
    """

    def test_reassigned_renders_from_and_to_owners(self) -> None:
        action = _disposition_action(
            reason="unassigned",
            payload_extra={"old_owner": "alice", "new_owners": ["bob"]},
        )
        zones = zones_for([action], colorize=False)
        blob = "".join(item if isinstance(item, str) else item.text for item in zones.action_needed)
        assert "reassigned (from alice → to bob):" in blob, repr(blob)
        assert "#77" in blob

    def test_reassigned_joins_multiple_new_owners(self) -> None:
        action = _disposition_action(
            reason="unassigned",
            payload_extra={"old_owner": "alice", "new_owners": ["bob", "carol"]},
        )
        zones = zones_for([action], colorize=False)
        blob = "".join(item if isinstance(item, str) else item.text for item in zones.action_needed)
        assert "reassigned (from alice → to bob, carol):" in blob, repr(blob)

    def test_reassigned_without_owner_data_is_still_labelled(self) -> None:
        """Old signals (no owner fields) must not regress to a bare token."""
        action = _disposition_action(reason="unassigned", payload_extra={})
        zones = zones_for([action], colorize=False)
        blob = "".join(item if isinstance(item, str) else item.text for item in zones.action_needed)
        assert "reassigned:" in blob, repr(blob)


_OPERATOR_ALIASES: tuple[tuple[str, ...], ...] = (("souliane", "op-alt", "op.work", "op@example.com"),)


class TestCanonicalIdentity:
    def test_handle_outside_every_group_is_its_own_canonical(self) -> None:
        from teatree.loop.rendering_classification import _CanonicalIdentity  # noqa: PLC0415

        identity = _CanonicalIdentity(_OPERATOR_ALIASES)
        assert identity.of("colleague") == "colleague"
        assert identity.of("op-alt") == "souliane"

    def test_empty_group_is_skipped(self) -> None:
        from teatree.loop.rendering_classification import _CanonicalIdentity  # noqa: PLC0415

        identity = _CanonicalIdentity(((), ("a", "b")))
        assert identity.of("a") == "a"
        assert identity.of("b") == "a"

    def test_blank_owner_is_never_a_self_handoff(self) -> None:
        from teatree.loop.rendering_classification import _CanonicalIdentity  # noqa: PLC0415

        identity = _CanonicalIdentity(_OPERATOR_ALIASES)
        assert identity.is_self_handoff("", ("souliane",)) is False
        assert identity.is_self_handoff("souliane", ()) is False


class TestSelfReassignmentSuppression:
    """Self-reassignments between the operator's own aliases never render."""

    def test_self_reassignment_is_suppressed(self) -> None:
        action = _disposition_action(
            reason="unassigned",
            payload_extra={"old_owner": "op-alt", "new_owners": ["souliane"]},
        )
        zones = zones_for([action], colorize=False, identity_aliases=_OPERATOR_ALIASES)
        blob = "".join(item if isinstance(item, str) else item.text for item in zones.action_needed)
        assert "reassigned" not in blob, repr(blob)

    def test_feed_of_only_self_reassignments_renders_zero_reassignment_output(self) -> None:
        actions = [
            _disposition_action(reason="unassigned", payload_extra={"old_owner": "op-alt", "new_owners": ["souliane"]}),
            _disposition_action(
                reason="unassigned", payload_extra={"old_owner": "op.work", "new_owners": ["souliane"]}
            ),
            _disposition_action(
                reason="unassigned", payload_extra={"old_owner": "souliane", "new_owners": ["op.work"]}
            ),
        ]
        zones = zones_for(actions, colorize=False, identity_aliases=_OPERATOR_ALIASES)
        blob = "".join(item if isinstance(item, str) else item.text for item in zones.action_needed)
        assert "reassigned" not in blob, repr(blob)

    def test_cross_human_reassignment_still_renders(self) -> None:
        action = _disposition_action(
            reason="unassigned",
            payload_extra={"old_owner": "souliane", "new_owners": ["colleague"]},
        )
        zones = zones_for([action], colorize=False, identity_aliases=_OPERATOR_ALIASES)
        blob = "".join(item if isinstance(item, str) else item.text for item in zones.action_needed)
        assert "reassigned (from souliane → to colleague):" in blob, repr(blob)

    def test_alias_collapses_to_canonical_display(self) -> None:
        action = _disposition_action(
            reason="unassigned",
            payload_extra={"old_owner": "op-alt", "new_owners": ["colleague"]},
        )
        zones = zones_for([action], colorize=False, identity_aliases=_OPERATOR_ALIASES)
        blob = "".join(item if isinstance(item, str) else item.text for item in zones.action_needed)
        assert "reassigned (from souliane → to colleague):" in blob, repr(blob)
        assert "op-alt" not in blob, repr(blob)


# The reported-noise shape: one human owning three handles (a GitHub login,
# a GitLab username, and the canonical name). The real handles are personal
# accounts kept out of this public repo; the structure is what matters.
_REPORTED_ALIASES: tuple[tuple[str, ...], ...] = (("souliane", "op-gh", "op-gl"),)


class TestReportedReassignNoise:
    """The user-visible noise: intra-self reassigns leaking + duplicated per source handle.

    Reproduces the reported statusline::

        reassigned (from op-gh → to souliane): #12 ·
        reassigned (from op-gh → to souliane): #69 ·
        reassigned (from op-gl → to souliane): #12 ·
        reassigned (from op-gl → to souliane): #69 · ...

    All three handles are one human, so every one of those rows is a no-op
    self-handoff and must vanish.
    """

    def test_all_intra_self_reassigns_render_zero_output(self) -> None:
        actions = [
            _disposition_action(
                reason="unassigned",
                payload_extra={"old_owner": old, "new_owners": ["souliane"]},
                ticket_number=num,
            )
            for old in ("op-gh", "op-gl")
            for num in ("12", "69")
        ]
        zones = zones_for(actions, colorize=False, identity_aliases=_REPORTED_ALIASES)
        blob = "".join(item if isinstance(item, str) else item.text for item in zones.action_needed)
        assert "reassigned" not in blob, repr(blob)

    def test_mixed_feed_shows_only_boundary_crossing_deduped(self) -> None:
        actions = [
            # Same ticket #12 surfaced twice, each row crossing the self
            # boundary from a DISTINCT non-self source owner — the two rows
            # stay distinct even after canonicalization, so the equality-based
            # dedup keeps both. A ticket is one observable thing regardless of
            # which source handle the reassign came from: dedup to one row.
            _disposition_action(
                reason="unassigned",
                payload_extra={"old_owner": "colleague-a", "new_owners": ["souliane"]},
                ticket_number="12",
            ),
            _disposition_action(
                reason="unassigned",
                payload_extra={"old_owner": "colleague-b", "new_owners": ["souliane"]},
                ticket_number="12",
            ),
            # Pure intra-self handoff — must be suppressed entirely.
            _disposition_action(
                reason="unassigned",
                payload_extra={"old_owner": "op-gh", "new_owners": ["souliane"]},
                ticket_number="69",
            ),
        ]
        zones = zones_for(actions, colorize=False, identity_aliases=_REPORTED_ALIASES)
        blob = "".join(item if isinstance(item, str) else item.text for item in zones.action_needed)
        assert blob.count("reassigned") == 1, repr(blob)
        assert "#12" in blob, repr(blob)
        assert "#69" not in blob, repr(blob)
        assert "op-gh" not in blob, repr(blob)
        assert "op-gl" not in blob, repr(blob)


def _stale_action(*, number: str, state: str, age: int, overlay: str = "teatree") -> DispatchAction:
    """Mirror dispatch output for a ``ticket.stale`` signal."""
    return DispatchAction(
        kind="statusline",
        zone="action_needed",
        detail=f"#{number} stale ({age}d)",
        payload={
            "stale": True,
            "overlay": overlay,
            "ticket_number": number,
            "ticket_state": state,
            "age_days": age,
            "issue_url": f"https://example.com/issues/{number}",
        },
    )


class TestStaleTicketsConciseAndLinked:
    """Asks 2 & 3 — stale tickets collapse to one concise, linked line.

    Before: one verbose unlinked ``TICKET-N stale in STATE (3d)`` line per
    ticket (the red sprawl). After: a single ``N stale: #a #b #c`` row per
    overlay with every ref a clickable link.
    """

    def test_multiple_stale_tickets_collapse_to_one_line(self) -> None:
        actions = [
            _stale_action(number="58", state="coded", age=4),
            _stale_action(number="724", state="started", age=6),
            _stale_action(number="878", state="tested", age=9),
        ]
        zones = zones_for(actions, colorize=False)
        # One line for the overlay, not three.
        assert len(zones.action_needed) == 1, repr(zones.action_needed)
        line = zones.action_needed[0]
        text = line if isinstance(line, str) else line.text
        assert "[teatree] 3 stale:" in text, repr(text)
        for ref in ("#58", "#724", "#878"):
            assert ref in text, repr(text)
        # Concise: the verbose per-ticket phrasing must be gone.
        assert "stale in" not in text

    def test_stale_refs_are_clickable_links(self) -> None:
        zones = zones_for([_stale_action(number="58", state="coded", age=4)], colorize=False)
        line = zones.action_needed[0]
        text = line if isinstance(line, str) else line.text
        # NO_COLOR fallback form proves the URL is attached to the ref.
        assert "#58 <https://example.com/issues/58>" in text, repr(text)

    def test_stale_lines_split_per_overlay(self) -> None:
        actions = [
            _stale_action(number="58", state="coded", age=4, overlay="teatree"),
            _stale_action(number="9", state="coded", age=5, overlay="acme"),
        ]
        zones = zones_for(actions, colorize=False)
        texts = sorted(item if isinstance(item, str) else item.text for item in zones.action_needed)
        assert any(t.startswith("[acme] 1 stale:") for t in texts), repr(texts)
        assert any(t.startswith("[teatree] 1 stale:") for t in texts), repr(texts)

    def test_stale_osc8_hyperlink_when_colorized(self) -> None:
        zones = zones_for([_stale_action(number="58", state="coded", age=4)], colorize=True)
        line = zones.action_needed[0]
        text = line if isinstance(line, str) else line.text
        assert "\033]8;;https://example.com/issues/58" in text, repr(text)


def _dm_action(
    *,
    ts: str,
    text: str,
    overlay: str = "teatree",
    channel: str = "D123",
    permalink: str = "",
) -> DispatchAction:
    """Mirror dispatch output for a ``slack.dm`` signal (#1050).

    The ``slack_mentions`` scanner emits ``summary=f"DM {ts}: {text[:80]}"``
    and ``payload={"ts": ts, "event": event}``; ``_run_job`` stamps
    ``overlay`` onto the payload; the dispatcher mirrors the summary into
    ``detail`` and routes ``zone="action_needed"``.
    """
    return DispatchAction(
        kind="statusline",
        zone="action_needed",
        detail=f"DM {ts}: {text[:80]}",
        payload={
            "ts": ts,
            "event": {"text": text, "ts": ts, "channel": channel},
            "overlay": overlay,
            "permalink": permalink,
        },
    )


class TestInboundDmsCollapseToOneNeutralLine:
    """Inbound DMs render as one dim line per overlay with permalinks only (#1050).

    Before: each DM was a separate red line with the body pasted in
    (``[ov] DM <ts>: <body>…``) — three sources of noise per overlay
    (red color, repeated lines, repeated body text the user reads in
    Slack natively). After: one ``[ov] DMs (N): <permalink1> · …`` line
    in the dim/anchors palette per overlay; permalinks only, no body.
    """

    def test_multiple_dms_collapse_to_one_line_per_overlay(self) -> None:
        actions = [
            _dm_action(
                ts="1779180869.828769",
                text=":white_check_mark: Bridge fix shipped — full status",
                permalink="https://slk.example/archives/D123/p1779180869828769",
            ),
            _dm_action(
                ts="1779180852.373219",
                text="you will use my reactions to the threads on the bot",
                permalink="https://slk.example/archives/D123/p1779180852373219",
            ),
            _dm_action(
                ts="1779180760.090199",
                text="I see that you don't create the snapshots before compacting",
                permalink="https://slk.example/archives/D123/p1779180760090199",
            ),
        ]
        zones = zones_for(actions, colorize=False)
        # Three DMs collapse to one line.
        action_lines = [item if isinstance(item, str) else item.text for item in zones.action_needed]
        anchor_lines = [item if isinstance(item, str) else item.text for item in zones.anchors]
        dm_lines = [t for t in anchor_lines + action_lines if "DM" in t]
        assert len(dm_lines) == 1, repr({"anchors": anchor_lines, "action_needed": action_lines})
        line = dm_lines[0]
        assert "[teatree] DMs (3):" in line, repr(line)

    def test_dm_line_contains_permalinks_not_body_text(self) -> None:
        body = ":white_check_mark: Bridge fix shipped — full status_  (idempotency key `slack-d"
        actions = [
            _dm_action(
                ts="1779180869.828769",
                text=body,
                permalink="https://slk.example/archives/D123/p1779180869828769",
            ),
        ]
        zones = zones_for(actions, colorize=False)
        blob = "".join(
            item if isinstance(item, str) else item.text
            for zone in (zones.anchors, zones.action_needed, zones.in_flight)
            for item in zone
        )
        # The Slack-message body must NOT appear in the statusline.
        assert "Bridge fix shipped" not in blob, repr(blob)
        assert "idempotency key" not in blob
        assert "white_check_mark" not in blob
        # The permalink must appear.
        assert "https://slk.example/archives/D123/p1779180869828769" in blob

    def test_dm_line_is_dim_not_red(self, tmp_path: Path) -> None:
        r"""DMs land in the anchors zone (dim) — never in action_needed (red).

        The anchors zone uses ``\033[38;5;244m`` (the same palette as
        ``started:``/``tested:`` rows); ``action_needed`` uses
        ``\033[1;31m`` (red). Routing DMs to anchors picks up the dim
        color automatically through ``_ZONE_COLORS``.
        """
        actions = [
            _dm_action(
                ts="1779180869.828769",
                text="any body",
                permalink="https://slk.example/archives/D123/p1779180869828769",
            ),
        ]
        zones = zones_for(actions, colorize=True)
        # Render to file so we see the actual color escapes applied.
        target = tmp_path / "statusline.txt"
        render(zones, target=target, colorize=True)
        content = target.read_text(encoding="utf-8")
        dm_lines = [line for line in content.splitlines() if "DMs (" in line]
        assert dm_lines, repr(content)
        for line in dm_lines:
            assert "\033[38;5;244m" in line, f"DM line missing dim color: {line!r}"
            assert "\033[1;31m" not in line, f"DM line uses red color: {line!r}"

    def test_dm_lines_split_per_overlay(self) -> None:
        actions = [
            _dm_action(ts="100.0", text="x", overlay="teatree", permalink="https://slk.example/x"),
            _dm_action(ts="200.0", text="y", overlay="acme", permalink="https://slk.example/y"),
            _dm_action(ts="201.0", text="z", overlay="acme", permalink="https://slk.example/z"),
        ]
        zones = zones_for(actions, colorize=False)
        all_items = [
            item if isinstance(item, str) else item.text
            for zone in (zones.anchors, zones.action_needed, zones.in_flight)
            for item in zone
        ]
        dm_lines = sorted(t for t in all_items if "DMs (" in t)
        assert len(dm_lines) == 2, repr(dm_lines)
        assert any(t.startswith("[acme] DMs (2):") for t in dm_lines), repr(dm_lines)
        assert any(t.startswith("[teatree] DMs (1):") for t in dm_lines), repr(dm_lines)

    def test_dm_permalinks_separated_by_middot(self) -> None:
        actions = [
            _dm_action(ts="100.0", text="a", permalink="https://slk.example/a"),
            _dm_action(ts="200.0", text="b", permalink="https://slk.example/b"),
        ]
        zones = zones_for(actions, colorize=False)
        blob = "".join(
            item if isinstance(item, str) else item.text
            for zone in (zones.anchors, zones.action_needed, zones.in_flight)
            for item in zone
        )
        dm_line = next(line for line in blob.splitlines() if "DMs (" in line)
        # Permalinks joined by " · " (same join character used elsewhere).
        assert " · " in dm_line, repr(dm_line)

    def test_dm_without_permalink_uses_ts_as_label(self) -> None:
        """Fall back to the bare timestamp when ``get_permalink`` returned empty.

        A Slack outage at scan time must not resurrect the old
        red-multi-line rendering — the renderer still produces a
        single dim DMs row using ``ts`` as label.
        """
        actions = [_dm_action(ts="1779180869.828769", text="x", permalink="")]
        zones = zones_for(actions, colorize=False)
        all_items = [
            item if isinstance(item, str) else item.text
            for zone in (zones.anchors, zones.action_needed, zones.in_flight)
            for item in zone
        ]
        dm_lines = [t for t in all_items if "DMs (" in t]
        assert dm_lines, repr(all_items)
        assert "[teatree] DMs (1):" in dm_lines[0], repr(dm_lines)
        # Body text must still be absent even in the fallback form.
        assert "x" not in dm_lines[0].split("DMs (1):")[1].split(" · ")[0].split()


class TestSlackUserReplyNeverRendersVerbatim:
    """#1113 Defect 2 — raw Slack reply text + ts must never leak verbatim.

    The full pipeline: a ``slack.user_reply`` signal → real ``dispatch()``
    → ``zones_for``. Pre-fix the signal fell through to the statusline
    fallback and ``c.other`` rendered ``Slack user reply <ts>: <text>``
    verbatim into the red zone.
    """

    def _signal(self) -> ScanSignal:
        return ScanSignal(
            kind="slack.user_reply",
            summary="Slack user reply 1779215938.999779: if there are posted in the channel",
            payload={
                "ts": "1779215938.999779",
                "channel": "C9XYZ",
                "user_id": "U123",
                "text": "if there are posted in the channel",
                "overlay": "t3-teatree",
            },
        )

    def test_slack_user_reply_text_and_ts_never_render_verbatim(self) -> None:
        blob = _render_blob(dispatch([self._signal()]))
        assert "1779215938.999779" not in blob, repr(blob)
        assert "if there are posted in the channel" not in blob, repr(blob)

    def test_render_defense_drops_a_statusline_slack_user_reply_action(self) -> None:
        """Defence-in-depth drops a stray ``slack.user_reply`` statusline shape.

        Even if the dispatcher regresses and emits a ``slack.user_reply``-shaped
        statusline action, the classifier must drop it before ``c.other``
        renders the raw text/ts verbatim.
        """
        action = DispatchAction(
            kind="statusline",
            zone="action_needed",
            detail="Slack user reply 1779.0001: some text",
            payload={
                "ts": "1779.0001",
                "channel": "C9",
                "user_id": "U1",
                "text": "some text",
                "overlay": "t3-teatree",
            },
        )
        blob = _render_blob([action])
        assert "1779.0001" not in blob, repr(blob)
        assert "some text" not in blob, repr(blob)


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestTicketExtraPrsResolvesMrToTicket:
    """#1113 Defect 3 — bare manually-opened MR buckets under its ticket.

    No ``PullRequest`` FK row, no ``Closes #N`` footer; the only link is
    ``Ticket.extra["prs"]["<url>"]``. Pre-fix ``build_ticket_index``
    returned ``{}`` and the MR rendered detached as an orphan
    ``[t3-teatree] !145`` row instead of nested under ``#142``.
    """

    URL = "https://gitlab.com/souliane/teatree/-/merge_requests/145"

    def _seed_ticket(self) -> None:
        from teatree.core.models.ticket import Ticket  # noqa: PLC0415

        Ticket.objects.create(
            overlay="t3-teatree",
            issue_url="https://github.com/souliane/teatree/issues/142",
            state=Ticket.State.STARTED,
            extra={"prs": {self.URL: {"iid": 145, "state": "opened"}}},
        )

    def _actions(self) -> list[DispatchAction]:
        return [
            DispatchAction(
                kind="statusline",
                zone="in_flight",
                detail="PR #145 open",
                payload={"url": self.URL, "iid": 145, "overlay": "t3-teatree"},
            ),
        ]

    def test_build_ticket_index_resolves_via_ticket_extra_prs(self) -> None:
        from teatree.loop.pr_ticket_index import build_ticket_index  # noqa: PLC0415

        self._seed_ticket()
        assert build_ticket_index(self._actions()).get(self.URL) == "142"

    def test_render_buckets_bare_mr_under_parent_ticket(self) -> None:
        self._seed_ticket()
        blob = _render_blob(self._actions())
        assert "#142" in blob, repr(blob)
        assert "(!145)" in blob or "!145" in blob, repr(blob)
        assert "\n[t3-teatree] !145" not in blob, repr(blob)


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestPostedReviewRequestPermalinkNotInChip:
    """#1377: the chip is bare — review-permalink suffix removed from the chip.

    A ``ReviewRequestPost`` row still records the review-channel post's
    channel + thread ts (the data path is unchanged), but the statusline
    chip no longer surfaces the permalink in-line. Per the binding spec
    the chip is just ``!<iid>``; richer per-MR signal belongs in
    dedicated zones, not on the chip.
    """

    URL = "https://gitlab.com/souliane/teatree/-/merge_requests/145"

    def _seed(self) -> None:
        from teatree.core.models.review_request_post import ReviewRequestPost  # noqa: PLC0415
        from teatree.core.models.ticket import Ticket  # noqa: PLC0415

        Ticket.objects.create(
            overlay="t3-teatree",
            issue_url="https://github.com/souliane/teatree/issues/142",
            state=Ticket.State.STARTED,
            extra={"prs": {self.URL: {"iid": 145, "state": "opened"}}},
        )
        ReviewRequestPost.objects.create(
            mr_url=self.URL,
            slack_channel_id="C9",
            slack_thread_ts="1779.0001",
        )

    def _actions(self) -> list[DispatchAction]:
        return [
            DispatchAction(
                kind="statusline",
                zone="in_flight",
                detail="PR #145 open",
                payload={"url": self.URL, "iid": 145, "overlay": "t3-teatree"},
            ),
        ]

    def test_chip_has_no_slack_permalink_suffix(self) -> None:
        self._seed()
        blob = _render_blob(self._actions())
        assert "!145" in blob, repr(blob)
        # Slack permalink suffix removed from the chip in #1377.
        assert "slack.com/archives/C9" not in blob, repr(blob)
        assert "(review" not in blob, repr(blob)


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestNoAgentsLineInInFlight:
    """The ``[ov] agents: …`` row is gone post-#1156.

    The line summarising CLAIMED-task phases per overlay duplicated state
    already visible in the active-tickets anchor and consumed a row of
    vertical space. Removed to make room for the per-loop dim anchors.
    """

    def test_no_agents_line_in_in_flight(self) -> None:
        from teatree.core.models import Task  # noqa: PLC0415
        from teatree.core.models.session import Session  # noqa: PLC0415
        from teatree.core.models.ticket import Ticket  # noqa: PLC0415

        ticket = Ticket.objects.create(
            overlay="teatree",
            issue_url="https://example.com/issues/777",
            state=Ticket.State.STARTED,
        )
        session = Session.objects.create(ticket=ticket, agent_id="coding")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            phase="coding",
            execution_target=Task.ExecutionTarget.HEADLESS,
        )
        task.claim(claimed_by="coding-worker")

        zones = zones_for([], colorize=False)
        blob = "\n".join(
            item if isinstance(item, str) else item.text
            for zone in (zones.anchors, zones.action_needed, zones.in_flight)
            for item in zone
        )
        assert "agents:" not in blob, repr(blob)


def test_canonical_item_drops_review_permalink_chunk() -> None:
    """#1377: canonical item omits the ``(review)`` permalink suffix.

    Even when a child MR has a ``review_permalink`` recorded, the chip
    renders as a bare ``!<iid>`` — the Slack permalink does not appear
    on the chip. Per the binding spec the chip is just the number;
    richer per-MR signal belongs in dedicated zones.
    """
    from teatree.loop.rendering_items import _LinkCtx, _PRRef, _render_canonical_item  # noqa: PLC0415

    def _link(text: str, url: object, *, colorize: bool) -> str:
        _ = colorize
        return f"{text} <{url}>" if isinstance(url, str) and url else text

    rendered = _render_canonical_item(
        label="#142",
        url="https://x/issues/142",
        title="example",
        child_refs=[
            _PRRef(
                iid=145,
                url="https://x/mr/145",
                annotation="",
                review_permalink="https://slack.com/archives/C9/p17790001",
            ),
        ],
        ctx=_LinkCtx(colorize=False, link=_link),
    )
    assert "!145 <https://x/mr/145>" in rendered, rendered
    assert "slack.com" not in rendered, rendered
    assert "review !145" not in rendered, rendered
