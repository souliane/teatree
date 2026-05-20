"""Row-by-row audit of ``teatree.loop.rendering`` (#982).

Each test pins one observable behaviour of one statusline row type so a
regression cannot silently re-introduce a row that appears when it
shouldn't, disappears when it should appear, or shows stale/duplicate
content.

The audit covers the four row families the statusline renders:

* anchors  — active-ticket lines (``[ov] state: #N``)
* action  — dispositions, ready-to-start, stale, action PRs
* in-flight PR groups — open PRs from the user
* in-flight task rows — ``[ov] agents: <phase> · <phase>``

Tests lean integration: they drive real ``DispatchAction`` payloads
(shape mirrors what ``dispatch._dispatch_one`` builds) through
``zones_for`` and assert on the resulting plain text. The DB-backed
``_running_tasks_lines`` path uses Django ``TestCase`` with real
``Ticket``/``Session``/``Task`` rows.
"""

import django.test

from teatree.loop.dispatch import DispatchAction
from teatree.loop.rendering import zones_for


def _blob(zone: list[object]) -> str:
    return "\n".join(item if isinstance(item, str) else item.text for item in zone)


def _active(num: str, state: str, overlay: str = "teatree", issue_url: str = "") -> DispatchAction:
    return DispatchAction(
        kind="statusline",
        zone="anchors",
        detail=f"#{num} {state}",
        payload={
            "ticket_number": num,
            "state": state,
            "overlay": overlay,
            "issue_url": issue_url or f"https://example.com/issues/{num}",
        },
    )


def _disposition(reason: str, num: str = "77", overlay: str = "teatree", **extra: object) -> DispatchAction:
    return DispatchAction(
        kind="statusline",
        zone="action_needed",
        detail=f"Ticket {num} — {reason}",
        payload={
            "reason": reason,
            "overlay": overlay,
            "ticket_number": num,
            "issue_url": f"https://example.com/issues/{num}",
            **extra,
        },
    )


def _stale(num: str, state: str = "coded", age: int = 4, overlay: str = "teatree") -> DispatchAction:
    return DispatchAction(
        kind="statusline",
        zone="action_needed",
        detail=f"#{num} stale ({age}d)",
        payload={
            "stale": True,
            "overlay": overlay,
            "ticket_number": num,
            "ticket_state": state,
            "age_days": age,
            "issue_url": f"https://example.com/issues/{num}",
        },
    )


def _my_pr(iid: int, *, overlay: str = "teatree", zone: str = "in_flight", **extra: object) -> DispatchAction:
    return DispatchAction(
        kind="statusline",
        zone=zone,
        detail=f"PR !{iid}",
        payload={
            "iid": iid,
            "url": f"https://gitlab.example.com/g/p/-/merge_requests/{iid}",
            "overlay": overlay,
            **extra,
        },
    )


class TestActiveTicketAnchors:
    """Anchors render one ``[ov] state: #N`` line per overlay, deduped."""

    def test_renders_active_tickets_grouped_by_state(self) -> None:
        zones = zones_for(
            [_active("10", "coded"), _active("11", "coded"), _active("12", "started")],
            colorize=False,
        )
        text = _blob(zones.anchors)
        assert "[teatree]" in text
        # NO_COLOR embeds the URL as ``#N <url>``; assert the two tickets
        # appear under the same state group in order.
        assert "coded: #10 " in text
        assert "#11" in text
        assert "started: #12" in text
        # Both states present in one line per overlay.
        assert text.count("[teatree]") == 1

    def test_anchor_skips_noise_states(self) -> None:
        """``merged``/``shipped``/``retrospected`` are post-PR; never anchor.

        The active scanner doesn't filter them — the renderer does. If a
        future refactor moves ``_NOISE_STATES`` filtering somewhere these
        rows show up as anchors, the user sees ghost post-merge work
        forever.

        #1163 refinement 2 narrowed the noise set to TRULY-terminal states
        only — ``in_review`` and ``not_started`` are rich work states that
        the statusline now surfaces, so they are covered by the sibling
        ``test_statusline_refinements_1163`` cases instead.
        """
        zones = zones_for(
            [
                _active("1", "merged"),
                _active("2", "shipped"),
                _active("3", "retrospected"),
            ],
            colorize=False,
        )
        assert zones.anchors == [], repr(zones.anchors)

    def test_anchor_dedupes_repeat_signals_for_the_same_ticket(self) -> None:
        """Duplicate ``ticket.active`` signals must collapse to one ref.

        Two scanner runs in one tick (rare but possible — e.g. an active
        ticket whose issue_url also matches a stale-tickets candidate)
        emit the same ``(num, state, issue_url)`` twice; the row must
        not duplicate ``#10 #10``.
        """
        zones = zones_for([_active("10", "coded"), _active("10", "coded")], colorize=False)
        text = _blob(zones.anchors)
        assert text.count("#10") == 1, repr(text)

    def test_anchor_hides_pr_backed_ticket_when_pr_not_live(self) -> None:
        """A ticket whose ``issue_url`` is a merged/closed MR URL is stale.

        The renderer drops it because no live action_prs / inflight_prs
        carry that URL. This is the stale-anchor filter that keeps merged
        work from lingering as a red anchor forever.
        """
        url = "https://gitlab.example.com/g/p/-/merge_requests/42"
        zones = zones_for([_active("42", "coded", issue_url=url)], colorize=False)
        assert zones.anchors == []

    def test_anchor_shows_pr_backed_ticket_when_pr_is_live(self) -> None:
        url = "https://gitlab.example.com/g/p/-/merge_requests/42"
        zones = zones_for(
            [_active("42", "coded", issue_url=url), _my_pr(42, zone="in_flight")],
            colorize=False,
        )
        text = _blob(zones.anchors)
        assert "#42" in text, repr(text)


class TestDispositionRows:
    """Action-needed dispositions: ``closed``, ``reassigned``, ``label-removed``."""

    def test_issue_closed_renders_clickable_ticket_ref(self) -> None:
        zones = zones_for([_disposition("issue_closed", num="55")], colorize=False)
        text = _blob(zones.action_needed)
        assert "closed:" in text
        assert "#55" in text

    def test_label_removed_renders_clickable_ticket_ref(self) -> None:
        zones = zones_for([_disposition("label_removed", num="56")], colorize=False)
        text = _blob(zones.action_needed)
        assert "label-removed:" in text
        assert "#56" in text

    def test_disposition_groups_same_reason_across_tickets(self) -> None:
        zones = zones_for(
            [_disposition("label_removed", num="56"), _disposition("label_removed", num="57")],
            colorize=False,
        )
        text = _blob(zones.action_needed)
        assert "label-removed:" in text, repr(text)
        assert "#56" in text, repr(text)
        assert "#57" in text, repr(text)
        # Both refs must share one ``label-removed:`` row (one prefix).
        assert text.count("label-removed:") == 1, repr(text)

    def test_disposition_dedupes_repeated_emission_for_same_ticket(self) -> None:
        """Two emissions of the same disposition+ticket must render once.

        Otherwise a flaky scanner produces ``label-removed: #56 #56`` and
        the operator can't tell whether two distinct events fired.
        """
        zones = zones_for(
            [_disposition("label_removed", num="56"), _disposition("label_removed", num="56")],
            colorize=False,
        )
        text = _blob(zones.action_needed)
        assert text.count("#56") == 1, repr(text)


class TestStaleRowDedup:
    """``ticket.stale`` collapse to ``N stale: #a #b #c`` once per overlay."""

    def test_stale_dedupes_repeated_emission_for_same_ticket(self) -> None:
        """One ticket emitted twice as stale renders as ``1 stale: #N`` once."""
        zones = zones_for([_stale("58"), _stale("58")], colorize=False)
        text = _blob(zones.action_needed)
        assert text.count("#58") == 1, repr(text)
        # Crucially the prefix count is "1 stale", not "2 stale".
        assert "1 stale:" in text, repr(text)


class TestInflightPrRows:
    """In-flight PR rows render ``[ov] !N`` per open PR."""

    def test_in_flight_pr_renders_iid_under_overlay(self) -> None:
        zones = zones_for([_my_pr(123)], colorize=False)
        text = _blob(zones.in_flight)
        assert "[teatree] !123" in text, repr(text)

    def test_in_flight_pr_dedupes_repeated_signal(self) -> None:
        """Two signals for the same PR collapse to one ``!N`` ref.

        ``MyPrsScanner`` and ``ReviewerPrsScanner`` can both surface the
        same PR (user is author AND a reviewer is requested). The
        statusline must not show ``!123 !123``.
        """
        zones = zones_for([_my_pr(123), _my_pr(123)], colorize=False)
        text = _blob(zones.in_flight)
        assert text.count("!123") == 1, repr(text)

    def test_in_flight_pr_failed_annotation_renders_once(self) -> None:
        zones = zones_for([_my_pr(456, zone="action_needed", status="failed")], colorize=False)
        text = _blob(zones.action_needed)
        # #1156: NO_COLOR renders ``!N <url>`` plus the annotation chunk.
        assert "!456" in text, repr(text)
        assert "(pipeline failed)" in text, repr(text)
        assert text.count("(pipeline failed)") == 1, repr(text)


class TestNoRunningTasksLine(django.test.TestCase):
    """The DB-backed ``agents:`` row is gone post-#1156.

    Pre-#1156 the renderer surfaced one ``[ov] agents: <phase>`` line
    per overlay summarising CLAIMED-task phases. That row duplicated
    state already visible in the active-tickets anchor and consumed a
    line of vertical space; it was removed to make room for the
    per-loop dim anchors.
    """

    def test_claimed_task_renders_no_agents_row(self) -> None:
        from teatree.core.models import Session, Task, Ticket  # noqa: PLC0415

        ticket = Ticket.objects.create(overlay="acme", issue_url="https://x/2", state="started")
        session = Session.objects.create(ticket=ticket, agent_id="a", overlay="acme")
        Task.objects.create(ticket=ticket, session=session, phase="coding", status=Task.Status.CLAIMED)

        zones = zones_for([], colorize=False)
        text = _blob(zones.in_flight)
        assert "agents:" not in text, repr(text)
