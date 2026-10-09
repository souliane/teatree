"""Statusline terse-chips spec (#1377 item shape, #130 state labels).

Pins the literal user-spec item shape ``#N (topic !chip1 !chip2 …)``,
prefixed by its FSM ``state:`` group label (#130):

- Topic and chips share ONE pair of parentheses.
- Chips are bare ``!<iid>`` (GitLab MR) or ``#<n>`` (GitHub PR) — no
    per-MR title chunk, no annotation chunk, no review-permalink suffix.
- No empty ``()`` when both topic and chips are absent — the parens
    are suppressed entirely.
- The line is grouped by FSM state, so a single ``work_started`` ticket reads
    ``[overlay] work_started: #N (topic !chips)``.

Example shape (overlay name sanitised for this public repo):
``[acme-fleet] work_started: #9112
    (green gizmo !9104 !9113 !9114 !9115)``
"""

import re

from teatree.loop.dispatch import DispatchAction
from teatree.loop.rendering import zones_for


def _blob(zone: list[object]) -> str:
    return "\n".join(item if isinstance(item, str) else item.text for item in zone)


def _active(num: str, *, title: str, overlay: str, issue_url: str) -> DispatchAction:
    return DispatchAction(
        kind="statusline",
        zone="anchors",
        detail=f"#{num} started",
        payload={
            "ticket_number": num,
            "state": "work_started",
            "overlay": overlay,
            "issue_url": issue_url,
            "title": title,
        },
    )


def _pr(*, iid: int, url: str, overlay: str, ticket_num: str) -> DispatchAction:
    return DispatchAction(
        kind="statusline",
        zone="in_flight",
        detail=f"PR !{iid}",
        payload={
            "iid": iid,
            "url": url,
            "overlay": overlay,
            "raw": {"description": f"Closes #{ticket_num}"},
        },
    )


def _visible(text: str) -> str:
    """Recover the bare visible glyphs a user reads in their terminal.

    Strips OSC8 hyperlink wrappers and ANSI CSI escapes so the assertion
    sees only what the user actually sees — the link URL lives inside
    the OSC8 envelope, not in the visible text.
    """
    text = re.sub(r"\x1b\]8;;[^\x1b\x07]*(?:\x1b\\|\x07)", "", text)
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)


class TestAnchorWithFourGitlabMrsMatchesShape:
    """The anchor must render `[overlay] #N (topic !a !b !c !d)` exactly."""

    def test_overlay_ticket_9112_with_four_gitlab_mrs(self) -> None:
        ticket_url = "https://gitlab.example.com/acme-fleet/-/issues/9112"
        base = "https://gitlab.example.com/acme-fleet/-/merge_requests"
        actions = [
            _active(
                "9112",
                title="green gizmo",
                overlay="acme-fleet",
                issue_url=ticket_url,
            ),
            _pr(iid=9104, url=f"{base}/9104", overlay="acme-fleet", ticket_num="9112"),
            _pr(iid=9113, url=f"{base}/9113", overlay="acme-fleet", ticket_num="9112"),
            _pr(iid=9114, url=f"{base}/9114", overlay="acme-fleet", ticket_num="9112"),
            _pr(iid=9115, url=f"{base}/9115", overlay="acme-fleet", ticket_num="9112"),
        ]
        zones = zones_for(actions, colorize=True)
        anchor = _visible(_blob(zones.anchors))
        anchor_line = next((line for line in anchor.splitlines() if line.startswith("[acme-fleet]")), "")
        expected = "[acme-fleet] work_started: #9112 (green gizmo !9104 !9113 !9114 !9115)"  # privacy-scan:allow
        assert anchor_line == expected, repr(anchor_line)


class TestGithubChipsUseHash:
    """A teatree ticket with two GitHub PRs renders ``#N`` chips, not ``!N``."""

    def test_teatree_ticket_with_two_github_prs_renders_hash_chips(self) -> None:
        ticket_url = "https://github.com/souliane/teatree/issues/97"
        actions = [
            _active(
                "97",
                title="terse chips spec",
                overlay="teatree",
                issue_url=ticket_url,
            ),
            _pr(
                iid=1377,
                url="https://github.com/souliane/teatree/pull/1377",
                overlay="teatree",
                ticket_num="97",
            ),
            _pr(
                iid=1399,
                url="https://github.com/souliane/teatree/pull/1399",
                overlay="teatree",
                ticket_num="97",
            ),
        ]
        zones = zones_for(actions, colorize=True)
        anchor = _visible(_blob(zones.anchors))
        anchor_line = next((line for line in anchor.splitlines() if line.startswith("[teatree]")), "")
        assert anchor_line == "[teatree] work_started: #97 (terse chips spec #1377 #1399)", repr(anchor_line)


class TestNoMrsNoEmptyParens:
    """A ticket with no MRs renders ``[ov] #N (topic)`` (no trailing space, no empty parens)."""

    def test_ticket_with_topic_and_no_mrs_has_no_trailing_decoration(self) -> None:
        actions = [
            _active(
                "100",
                title="some topic",
                overlay="acme-fleet",
                issue_url="https://gitlab.example.com/acme-fleet/-/issues/100",
            ),
        ]
        zones = zones_for(actions, colorize=True)
        anchor = _visible(_blob(zones.anchors))
        anchor_line = next((line for line in anchor.splitlines() if line.startswith("[acme-fleet]")), "")
        assert anchor_line == "[acme-fleet] work_started: #100 (some topic)", repr(anchor_line)

    def test_ticket_with_no_topic_and_no_mrs_has_no_parens_at_all(self) -> None:
        actions = [
            _active(
                "100",
                title="",
                overlay="acme-fleet",
                issue_url="https://gitlab.example.com/acme-fleet/-/issues/100",
            ),
        ]
        zones = zones_for(actions, colorize=True)
        anchor = _visible(_blob(zones.anchors))
        anchor_line = next((line for line in anchor.splitlines() if line.startswith("[acme-fleet]")), "")
        # ``[acme-fleet] work_started: #100`` — state label, then the bare ``#100``
        # (no parens, no trailing space when topic and chips are both absent).
        assert anchor_line == "[acme-fleet] work_started: #100", repr(anchor_line)
