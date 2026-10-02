"""Canonical statusline item shape: ``#N (short desc) (!M1, !M2)`` (#1015).

Every state line (anchor ``ready:``/``started:``/``tested:`` rows and the
action-needed ``ready:`` row) renders items in the same canonical shape so
the operator never has to mentally translate between two formats. The
description is a terse 2-3 word topic derived from the cached tracker title
(``ticket.extra["issue_title"]``) — the conventional-commit prefix stripped,
the first few words kept, capped with a Unicode ellipsis; the MR chunk is
space-separated and every number is a hyperlink.

These tests pin the canonical shape on the anchor row (replaces the old
``coded: #N`` bare form), the canonical shape on the ``ready:`` action row,
graceful degradation when ``title`` is empty (just ``#N (!M)``), the terse
topic collapse for long titles, and the ``ActiveTicketsScanner`` plumbing
``extra['issue_title']`` through the payload.
"""

from django.test import TestCase

from teatree.loop.dispatch import DispatchAction
from teatree.loop.rendering import zones_for
from teatree.loop.rendering_items import _short_desc


def _blob(zone: list[object]) -> str:
    return "\n".join(item if isinstance(item, str) else item.text for item in zone)


def _active(num: str, state: str, *, title: str = "", overlay: str = "teatree") -> DispatchAction:
    return DispatchAction(
        kind="statusline",
        zone="anchors",
        detail=f"#{num} {state}",
        payload={
            "ticket_number": num,
            "state": state,
            "overlay": overlay,
            "issue_url": f"https://example.com/issues/{num}",
            "title": title,
        },
    )


def _my_pr(iid: int, *, overlay: str = "teatree") -> DispatchAction:
    return DispatchAction(
        kind="statusline",
        zone="in_flight",
        detail=f"PR !{iid}",
        payload={
            "iid": iid,
            "url": f"https://gitlab.example.com/g/p/-/merge_requests/{iid}",
            "overlay": overlay,
        },
    )


def _ready(num: str, *, title: str = "", overlay: str = "teatree") -> DispatchAction:
    # ``issue_intake.admitted`` produces a payload WITHOUT ``reason`` so the
    # renderer takes the dedicated ``ready_refs`` branch (which is what
    # ``_render_action_line`` consumes for the canonical-shape ``ready:``
    # row). A payload with ``reason`` would be routed as a disposition.
    return DispatchAction(
        kind="statusline",
        zone="action_needed",
        detail=f"Ready to start: #{num}",
        payload={
            "ticket_number": num,
            "overlay": overlay,
            "issue_url": f"https://example.com/issues/{num}",
            "url": f"https://example.com/issues/{num}",
            "title": title,
        },
    )


class TestShortDescHelper:
    def test_passes_short_topics_through_unchanged(self) -> None:
        # A title already 2-3 words within budget reads verbatim.
        assert _short_desc("short title") == "short title"

    def test_returns_empty_for_empty_input(self) -> None:
        assert _short_desc("") == ""

    def test_collapses_long_titles_to_terse_topic(self) -> None:
        # A long single token is tail-elided at the terse 24-char budget,
        # not the prior 40-char commit-subject slice.
        long = "x" * 60
        out = _short_desc(long)
        assert len(out) <= 24
        assert out.endswith("…")
        assert out == "x" * 23 + "…"

    def test_keeps_first_three_words_only(self) -> None:
        # Beyond three words the topic is gist, not the full subject.
        assert _short_desc("add the canonical item shape everywhere") == "add the canonical"

    def test_strips_conventional_commit_prefix(self) -> None:
        assert _short_desc("feat(loop): multi loop anchors") == "multi loop anchors"
        assert _short_desc("techdebt: refactor module") == "refactor module"


class TestAnchorCanonicalShape:
    """Anchor state lines render ``#N (desc) (!M)`` consistently."""

    def test_anchor_renders_short_desc_in_canonical_shape(self) -> None:
        zones = zones_for(
            [_active("10", "coded", title="Add canonical item shape")],
            colorize=False,
        )
        text = _blob(zones.anchors)
        # #130 restores the FSM ``state:`` group label dropped by #1377;
        # the terse per-item shape ``#N (topic)`` is unchanged. The topic
        # keeps the first three words.
        assert "#10" in text
        assert "coded:" in text
        assert "(Add canonical item)" in text, repr(text)

    def test_anchor_collapses_long_titles_to_terse_topic(self) -> None:
        zones = zones_for(
            [_active("10", "coded", title="x" * 60)],
            colorize=False,
        )
        text = _blob(zones.anchors)
        assert "(" + ("x" * 23) + "…)" in text, repr(text)

    def test_anchor_no_desc_chunk_when_title_empty(self) -> None:
        zones = zones_for([_active("10", "coded", title="")], colorize=False)
        text = _blob(zones.anchors)
        # No parens after #10 when no title and no PRs.
        assert "#10 " in text
        # The state header is the only "(" before the URL.
        # Ensure no "()" (empty desc) bug.
        assert "()" not in text, repr(text)

    def test_anchor_renders_topic_and_chips_inside_one_paren_group(self) -> None:
        """#1377 binding spec: topic AND chips share ONE pair of parens.

        ``[overlay] #N (topic !M1 !M2)`` — not ``#N (topic) !M1 !M2``.
        Per the user-spec, every chip is a bare ``!<iid>`` — no per-MR
        title chunk, no annotation, no comma separator.
        """
        # ``build_ticket_index`` parses ``Closes #N`` from the MR
        # description (nested under ``payload['raw']['description']`` — the
        # signal-payload contract) to bucket MRs under their parent ticket.
        action_active = _active("44", "coded", title="Tickety tick")
        action_pr1 = DispatchAction(
            kind="statusline",
            zone="in_flight",
            detail="PR !1",
            payload={
                "iid": 1,
                "url": "https://gitlab.example.com/g/p/-/merge_requests/1",
                "overlay": "teatree",
                "raw": {"description": "Closes #44"},
            },
        )
        action_pr2 = DispatchAction(
            kind="statusline",
            zone="in_flight",
            detail="PR !2",
            payload={
                "iid": 2,
                "url": "https://gitlab.example.com/g/p/-/merge_requests/2",
                "overlay": "teatree",
                "raw": {"description": "Closes #44"},
            },
        )
        zones = zones_for([action_active, action_pr1, action_pr2], colorize=False)
        anchor = _blob(zones.anchors)
        assert "#44" in anchor, repr(anchor)
        # Topic and chips share ONE pair of parens — ``(Tickety tick) !1``
        # (chips outside) is the banned shape.
        assert "(Tickety tick) !1" not in anchor, repr(anchor)
        assert "(Tickety tick !1" in anchor, repr(anchor)
        assert "!1" in anchor, repr(anchor)
        assert "!2" in anchor, repr(anchor)
        assert ", !2" not in anchor, repr(anchor)

    def test_anchor_renders_canonical_shape_under_colorize(self) -> None:
        """Under OSC8 the chips stay inside the topic parens (#1377)."""
        action_active = _active("44", "coded", title="Tickety tick")
        action_pr1 = DispatchAction(
            kind="statusline",
            zone="in_flight",
            detail="PR !1",
            payload={
                "iid": 1,
                "url": "https://gitlab.example.com/g/p/-/merge_requests/1",
                "overlay": "teatree",
                "raw": {"description": "Closes #44"},
            },
        )
        action_pr2 = DispatchAction(
            kind="statusline",
            zone="in_flight",
            detail="PR !2",
            payload={
                "iid": 2,
                "url": "https://gitlab.example.com/g/p/-/merge_requests/2",
                "overlay": "teatree",
                "raw": {"description": "Closes #44"},
            },
        )
        zones = zones_for([action_active, action_pr1, action_pr2], colorize=True)
        anchor = _blob(zones.anchors)
        assert "!1" in anchor, repr(anchor)
        assert "!2" in anchor, repr(anchor)
        # The comma-joined form must NOT appear.
        assert ", !2" not in anchor, repr(anchor)


class TestCanonicalShapeSurvivesTickSplitMerge:
    """Post-#1054/#1061 tick-split merge regression for the canonical shape.

    The ``#N (desc) (!M1, !M2)`` shape must still render with clickable
    numbers, comma-joined MRs, and the description chunk omitted when
    the title is empty. This pins all three invariants in one row so a
    future merge that re-homes rendering can't silently regress any of
    them. Anti-vacuous: reverting any of the three #1015 rendering
    behaviours turns this RED.
    """

    def test_full_canonical_shape_with_clickable_numbers_and_inner_chips(self) -> None:
        import re  # noqa: PLC0415

        zones = zones_for(
            [
                _active("44", "coded", title="Tickety tick"),
                DispatchAction(
                    kind="statusline",
                    zone="in_flight",
                    detail="PR !1",
                    payload={
                        "iid": 1,
                        "url": "https://gitlab.example.com/g/p/-/merge_requests/1",
                        "overlay": "teatree",
                        "raw": {"description": "Closes #44"},
                    },
                ),
                DispatchAction(
                    kind="statusline",
                    zone="in_flight",
                    detail="PR !2",
                    payload={
                        "iid": 2,
                        "url": "https://gitlab.example.com/g/p/-/merge_requests/2",
                        "overlay": "teatree",
                        "raw": {"description": "Closes #44"},
                    },
                ),
            ],
            colorize=True,
        )
        anchor = _blob(zones.anchors)
        # (1) The ticket number is a clickable OSC8 hyperlink to the issue.
        assert "\033]8;;https://example.com/issues/44\033\\" in anchor, repr(anchor)
        assert "#44" in anchor, repr(anchor)
        # (2) Each MR number is its own clickable OSC8 hyperlink.
        assert "\033]8;;https://gitlab.example.com/g/p/-/merge_requests/1\033\\" in anchor, repr(anchor)
        assert "\033]8;;https://gitlab.example.com/g/p/-/merge_requests/2\033\\" in anchor, repr(anchor)
        # (3) Strip OSC8 sequences to recover the visible text and pin the
        #     #1377 terse shape: ``#44 (Tickety tick !1 !2)`` — topic and
        #     chips share ONE pair of parens.
        visible = re.sub(r"\033]8;;[^\033]*\033\\", "", anchor)
        assert re.search(r"#44 \(Tickety tick !1 !2\)", visible), repr(visible)

    def test_description_chunk_omitted_when_title_empty(self) -> None:
        import re  # noqa: PLC0415

        zones = zones_for(
            [
                _active("45", "coded", title=""),
                DispatchAction(
                    kind="statusline",
                    zone="in_flight",
                    detail="PR !3",
                    payload={
                        "iid": 3,
                        "url": "https://gitlab.example.com/g/p/-/merge_requests/3",
                        "overlay": "teatree",
                        "raw": {"description": "Closes #45"},
                    },
                ),
            ],
            colorize=False,
        )
        anchor = _blob(zones.anchors)
        visible = re.sub(r"\033]8;;[^\033]*\033\\", "", anchor)
        # No empty description parens: shape collapses to ``#45 (!3 …)``,
        # never ``#45 () (…)``.
        assert "#45 ()" not in visible, repr(visible)
        assert "#45" in visible, repr(visible)
        assert "!3" in visible, repr(visible)


class TestReadyRowCanonicalShape:
    def test_ready_row_renders_short_desc(self) -> None:
        zones = zones_for(
            [_ready("99", title="Add identity aliases")],
            colorize=False,
        )
        text = _blob(zones.action_needed)
        assert "ready:" in text
        assert "#99" in text
        assert "(Add identity aliases)" in text, repr(text)

    def test_ready_row_collapses_long_titles_to_terse_topic(self) -> None:
        zones = zones_for([_ready("99", title="x" * 60)], colorize=False)
        text = _blob(zones.action_needed)
        assert "(" + ("x" * 23) + "…)" in text, repr(text)

    def test_ready_row_falls_back_to_no_desc_when_title_missing(self) -> None:
        zones = zones_for([_ready("99", title="")], colorize=False)
        text = _blob(zones.action_needed)
        # No description chunk after #99.
        assert "ready:" in text
        assert "#99" in text
        assert "()" not in text, repr(text)


class TestActiveScannerPlumbsTitle(TestCase):
    """Plumbing: ``ActiveTicketsScanner`` must ship ``title`` on the signal.

    Without this the renderer has nothing to truncate and the canonical
    shape silently collapses to the legacy bare ``#N`` form.
    """

    def test_active_scanner_includes_title_from_extra(self) -> None:
        from teatree.core.models import Ticket  # noqa: PLC0415
        from teatree.loop.scanners.active_tickets import ActiveTicketsScanner  # noqa: PLC0415

        Ticket.objects.create(
            overlay="teatree",
            issue_url="https://example.com/issues/77",
            state="coded",
            extra={"issue_title": "Sample title"},
        )
        signals = ActiveTicketsScanner(overlay_name="teatree").scan()
        assert len(signals) == 1
        assert signals[0].payload["title"] == "Sample title"

    def test_active_scanner_defaults_title_to_empty_when_missing(self) -> None:
        from teatree.core.models import Ticket  # noqa: PLC0415
        from teatree.loop.scanners.active_tickets import ActiveTicketsScanner  # noqa: PLC0415

        Ticket.objects.create(
            overlay="teatree",
            issue_url="https://example.com/issues/78",
            state="coded",
            extra={},
        )
        signals = ActiveTicketsScanner(overlay_name="teatree").scan()
        assert signals[0].payload["title"] == ""
