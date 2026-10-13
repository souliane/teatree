import datetime as dt

from teatree.loop.scanners.issue_payload import issue_author, issue_created_at, issue_url

_URL = "https://github.com/acme/widget/issues/100"
_AUTHOR = "octo-author"


def test_issue_url_reads_both_forge_url_fields() -> None:
    assert issue_url({"web_url": _URL}) == _URL
    assert issue_url({"html_url": _URL}) == _URL
    assert issue_url({}) == ""


def test_issue_author_reads_both_forge_shapes_and_strips() -> None:
    assert issue_author({"user": {"login": f" {_AUTHOR} "}}) == _AUTHOR
    assert issue_author({"author": {"username": _AUTHOR}}) == _AUTHOR
    assert issue_author({"user": "not-a-dict"}) == ""


def test_issue_created_at_reads_both_forges_and_degrades_to_none() -> None:
    assert issue_created_at({"created_at": "2026-08-01T09:00:00Z"}) == dt.datetime(2026, 8, 1, 9, tzinfo=dt.UTC)
    # GitLab emits sub-second precision and a numeric offset.
    assert issue_created_at({"created_at": "2026-08-01T11:00:00.000+02:00"}) == dt.datetime(
        2026, 8, 1, 9, tzinfo=dt.UTC
    )
    # A naive stamp is read as UTC, not as local time.
    assert issue_created_at({"created_at": "2026-08-01T09:00:00"}) == dt.datetime(2026, 8, 1, 9, tzinfo=dt.UTC)
    assert issue_created_at({"created_at": "not a date"}) is None
    assert issue_created_at({"created_at": 1234}) is None
    assert issue_created_at({}) is None
