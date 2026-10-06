from pathlib import Path

import pytest

from teatree.browser.evidence import BrowserEvent, EventLog

_EVENTS = {
    "CONSOLE error boom": BrowserEvent("console", text="boom", level="error"),
    "CONSOLE log hello": BrowserEvent("console", text="hello", level="log"),
    "PAGEERROR TypeError: x": BrowserEvent("pageerror", text="TypeError: x"),
    "FAILED GET http://h/x — net::ERR_CONNECTION_REFUSED": BrowserEvent(
        "requestfailed", text="net::ERR_CONNECTION_REFUSED", url="http://h/x", method="GET"
    ),
    "HTTP 404 http://h/a.png": BrowserEvent("response", url="http://h/a.png", status=404),
    "HTTP 200 http://h/": BrowserEvent("response", url="http://h/", status=200),
    "NAVIGATED http://h/": BrowserEvent("navigation", url="http://h/"),
}


@pytest.mark.parametrize(("line", "event"), _EVENTS.items())
def test_each_event_renders_its_report_line(line: str, event: BrowserEvent) -> None:
    assert event.render() == line


def test_only_errors_failures_and_http_errors_are_findings() -> None:
    assert [line for line, event in _EVENTS.items() if event.is_finding] == [
        "CONSOLE error boom",
        "PAGEERROR TypeError: x",
        "FAILED GET http://h/x — net::ERR_CONNECTION_REFUSED",
        "HTTP 404 http://h/a.png",
    ]


def test_the_log_numbers_events_and_reads_after_a_sequence(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    for text in ("a", "b", "c"):
        log.append(BrowserEvent("console", text=text, level="log"))

    assert [event.text for event in log.read(after=1)] == ["b", "c"]
    assert EventLog(tmp_path / "events.jsonl").last_seq() == 3


def test_the_log_keeps_only_the_newest_events(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl", keep=3)
    for index in range(7):
        log.append(BrowserEvent("console", text=str(index), level="log"))

    assert [event.text for event in log.read()][-3:] == ["4", "5", "6"]
    assert len(log.read()) < 7
    assert log.last_seq() == 7


def test_inspection_reads_from_the_last_navigation(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    for event in (
        BrowserEvent("console", text="old", level="error"),
        BrowserEvent("navigation", url="http://h/"),
        BrowserEvent("console", text="new", level="error"),
    ):
        log.append(event)

    assert [event.text for event in log.since_last_navigation()] == ["new"]


def test_an_absent_log_is_empty(tmp_path: Path) -> None:
    assert EventLog(tmp_path / "missing.jsonl").read() == []
