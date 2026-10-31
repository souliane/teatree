# test-path: cross-cutting
"""Every ``HOOK_EVENTS`` member has a row in the Event table of ``docs/blueprint/loop-topology.md``."""

from pathlib import Path

from teatree.core.intake.github_payload import HOOK_EVENTS

_ROOT = Path(__file__).resolve().parents[2]
_HEADER = "| Event | Subscription | Current polling reads |"


def _event_rows() -> dict[str, tuple[str, str]]:
    document = (_ROOT / "docs/blueprint/loop-topology.md").read_text(encoding="utf-8")
    section = document.partition("**GitHub webhook event inventory.**")[2].partition("**Forge relay identity.**")[0]
    assert _HEADER in section
    table = section.partition(_HEADER)[2].partition("\n\n")[0]
    cells = (tuple(cell.strip() for cell in line.strip("|").split("|")) for line in table.splitlines())
    return {row[0].strip("`"): (row[1], row[2]) for row in cells if row[0].startswith("`")}


def test_every_hook_event_has_a_row_and_only_hook_events_are_subscribed() -> None:
    events = _event_rows()

    assert not set(HOOK_EVENTS) - set(events), "HOOK_EVENTS member with no row in the Event table"
    assert set(events) == set(HOOK_EVENTS) | {"check_run"}
    assert {event for event, (status, _) in events.items() if status == "Subscribed"} == set(HOOK_EVENTS)


def test_check_run_is_documented_as_legacy_only_with_its_polling_reads() -> None:
    status, reads = _event_rows()["check_run"]

    assert status == "Legacy only"
    assert {"GhMainCiStatus", "GhPrApiClient.main_check_failed", "red_set_surface"} <= set(
        reads.replace("`", "").replace(",", "").split()
    )


def test_issue_comment_row_names_the_conversation_comment_pollers() -> None:
    _, reads = _event_rows()["issue_comment"]

    assert {"ReviewNagScanner", "ReviewRequestResumeScanner", "review_taken_by_other"} <= set(
        reads.replace("`", "").replace(",", "").split()
    )
