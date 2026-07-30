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
    """Anchors render one terse line per overlay, deduped (#1377)."""

    def test_renders_active_tickets_in_terse_format(self) -> None:
        zones = zones_for(
            [_active("10", "coded"), _active("11", "coded"), _active("12", "started")],
            colorize=False,
        )
        text = _blob(zones.anchors)
        assert "[teatree]" in text
        # All surviving actively-shipping items appear on ONE line per
        # overlay, grouped by FSM state.
        assert "#10" in text
        assert "#11" in text
        assert "#12" in text
        # #130 restores the FSM ``state:`` group label (#1377 had dropped
        # it); tickets group by status, ordered by state priority.
        assert "coded:" in text
        assert "started:" in text
        assert text.index("started:") < text.index("coded:"), text
        # One line per overlay.
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
        # The configured-overlays summary line may be present; the audit here
        # is that no TICKET anchor row surfaces for the noise states.
        text = _blob(zones.anchors)
        assert "#1" not in text, repr(text)
        assert "#2" not in text, repr(text)
        assert "#3" not in text, repr(text)

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
        # The configured-overlays summary line may be present; the stale PR
        # ticket itself must not surface as an anchor.
        assert "#42" not in _blob(zones.anchors)

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

    def test_in_flight_pr_failed_renders_bare_chip_no_annotation(self) -> None:
        """Per #1377 the chip is bare ``!N`` — ``(pipeline failed)`` removed."""
        zones = zones_for([_my_pr(456, zone="action_needed", status="failed")], colorize=False)
        text = _blob(zones.action_needed)
        assert "!456" in text, repr(text)
        # Annotation decoration removed by #1377.
        assert "(pipeline failed)" not in text, repr(text)


def _orphaned(task_id: int, overlay: str = "teatree") -> DispatchAction:
    """Build a ``task.orphaned`` statusline action as the dispatcher emits it."""
    return DispatchAction(
        kind="statusline",
        zone="action_needed",
        detail=f"Task {task_id} — artifact state unverifiable, needs operator review",
        payload={
            "task_id": task_id,
            "ticket_id": task_id + 1000,
            "issue_url": f"https://example.com/issues/{task_id}",
            "overlay": overlay,
        },
    )


class TestOrphanedTaskCollapse:
    """N ``task.orphaned`` signals collapse to ONE summary line in action_needed."""

    def test_single_orphaned_task_renders_one_summary_line(self) -> None:
        zones = zones_for([_orphaned(42)], colorize=False)
        text = _blob(zones.action_needed)
        assert "task needs operator review" in text, repr(text)
        assert "1 task" in text, repr(text)

    def test_many_orphaned_tasks_render_exactly_one_summary_line(self) -> None:
        actions = [_orphaned(i) for i in range(1, 12)]
        zones = zones_for(actions, colorize=False)
        lines = [item if isinstance(item, str) else item.text for item in zones.action_needed]
        # Exactly one line mentions operator review — not 11 separate rows.
        review_lines = [ln for ln in lines if "operator review" in ln]
        assert len(review_lines) == 1, repr(lines)
        assert "11 tasks need operator review" in review_lines[0], repr(review_lines[0])

    def test_orphaned_task_summary_includes_count(self) -> None:
        actions = [_orphaned(i) for i in range(1, 4)]
        zones = zones_for(actions, colorize=False)
        text = _blob(zones.action_needed)
        assert "3 tasks need operator review" in text, repr(text)

    def test_orphaned_tasks_from_different_overlays_each_get_one_line(self) -> None:
        actions = [
            _orphaned(1, overlay="overlay-a"),
            _orphaned(2, overlay="overlay-a"),
            _orphaned(3, overlay="overlay-b"),
        ]
        zones = zones_for(actions, colorize=False)
        lines = [item if isinstance(item, str) else item.text for item in zones.action_needed]
        review_lines = [ln for ln in lines if "operator review" in ln]
        assert len(review_lines) == 2, repr(lines)
        assert any("overlay-a" in ln and "2 tasks" in ln for ln in review_lines), repr(review_lines)
        assert any("overlay-b" in ln and "1 task" in ln for ln in review_lines), repr(review_lines)


def _pending_task(
    task_id: int,
    phase: str = "short_describe",
    status: str = "pending",
    overlay: str = "teatree",
) -> DispatchAction:
    """Build a ``pending_task`` statusline action as the dispatcher emits it.

    A pending-task signal whose phase has no registered sub-agent falls
    through ``dispatch._dispatch_one`` to the ``in_flight`` statusline zone.
    """
    return DispatchAction(
        kind="statusline",
        zone="in_flight",
        detail=f"Task {task_id} ({phase}) {status}",
        payload={
            "task_id": task_id,
            "phase": phase,
            "status": status,
            "ticket_id": task_id + 1000,
            "overlay": overlay,
        },
    )


class TestPendingTaskGrouping:
    """Pending-task rows collapse to ONE line per status, not one per task."""

    def test_many_pending_tasks_render_one_line_per_status(self) -> None:
        actions = [_pending_task(i) for i in range(1, 51)]
        zones = zones_for(actions, colorize=False)
        lines = [item if isinstance(item, str) else item.text for item in zones.in_flight]
        task_lines = [ln for ln in lines if "teatree tasks:" in ln]
        assert len(task_lines) == 1, repr(lines)
        assert "pending: 50" in task_lines[0], repr(task_lines[0])

    def test_no_per_task_line_appears(self) -> None:
        actions = [_pending_task(i) for i in range(1, 51)]
        zones = zones_for(actions, colorize=False)
        text = _blob(zones.in_flight)
        assert "Task 1 " not in text, repr(text)
        assert "Task 30 " not in text, repr(text)

    def test_bare_phase_token_never_leaks_as_description(self) -> None:
        actions = [
            _pending_task(1, phase="short_describe"),
            _pending_task(2, phase="dogfood_smoke"),
            _pending_task(3, phase="architectural_review"),
        ]
        zones = zones_for(actions, colorize=False)
        text = _blob(zones.in_flight)
        assert "short_describe" not in text, repr(text)
        assert "dogfood_smoke" not in text, repr(text)
        assert "architectural_review" not in text, repr(text)

    def test_groups_by_distinct_status(self) -> None:
        actions = [
            *(_pending_task(i, status="pending") for i in range(1, 4)),
            _pending_task(10, status="claimed"),
            _pending_task(11, status="claimed"),
        ]
        zones = zones_for(actions, colorize=False)
        text = _blob(zones.in_flight)
        assert "pending: 3" in text, repr(text)
        assert "claimed: 2" in text, repr(text)

    def test_each_overlay_gets_its_own_grouped_line(self) -> None:
        actions = [
            _pending_task(1, overlay="overlay-a"),
            _pending_task(2, overlay="overlay-a"),
            _pending_task(3, overlay="overlay-b"),
        ]
        zones = zones_for(actions, colorize=False)
        lines = [item if isinstance(item, str) else item.text for item in zones.in_flight]
        task_lines = [ln for ln in lines if "teatree tasks:" in ln]
        assert len(task_lines) == 2, repr(lines)
        assert any("overlay-a" in ln and "pending: 2" in ln for ln in task_lines), repr(task_lines)
        assert any("overlay-b" in ln and "pending: 1" in ln for ln in task_lines), repr(task_lines)


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
