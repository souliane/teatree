from unittest.mock import Mock

from teatree.core.issue_writes.issue_candidate_selection import (
    description_of,
    issue_url_of,
    open_candidates,
    select_closest,
)


def test_closest_candidates_use_title_and_body_and_have_stable_url_ties() -> None:
    live = {
        f"https://github.com/acme/widgets/issues/{number}": {"title": "routine task", "body": "unrelated"}
        for number in range(21)
    }
    title_match = "https://github.com/acme/widgets/issues/21"
    body_match = "https://github.com/acme/widgets/issues/22"
    live[title_match] = {"title": "cache outage", "body": "unrelated"}
    live[body_match] = {"title": "routine task", "body": "cache outage details"}

    first = select_closest(live, "cache outage", "details")
    reversed_input = select_closest(dict(reversed(list(live.items()))), "cache outage", "details")

    assert len(first) == 20
    assert list(first) == list(reversed_input)
    assert list(first)[:2] == [title_match, body_match]
    assert (
        list(first)[2:]
        == sorted(set(live) - {title_match, body_match}, key=lambda url: int(url.rsplit("/", 1)[1]))[:18]
    )
    assert set(first) <= set(live)


def test_equal_scores_order_by_numeric_issue_number() -> None:
    base = "https://github.com/acme/widgets/issues/"
    live = {f"{base}{number}": {"title": "routine task", "body": "unrelated"} for number in (19, 10, 2)}

    assert list(select_closest(live, "cache outage", "details")) == [f"{base}{number}" for number in (2, 10, 19)]
    assert list(select_closest({f"{base}{name}": live[f"{base}2"] for name in ("beta", "alpha")}, "", "")) == [
        f"{base}alpha",
        f"{base}beta",
    ]


def test_closest_candidates_read_gitlab_description() -> None:
    live = {
        "https://gitlab.com/acme/widgets/-/issues/1": {"title": "routine task", "description": "cache outage"},
        "https://gitlab.com/acme/widgets/-/issues/2": {"title": "routine task", "description": "unrelated"},
    }

    assert list(select_closest(live, "cache outage", "cache outage", limit=1)) == [
        "https://gitlab.com/acme/widgets/-/issues/1"
    ]


def test_listing_skips_unaddressable_issues_and_handles_missing_text() -> None:
    host = Mock()
    url = "https://github.com/acme/widgets/issues/1"
    host.list_repo_open_issues.return_value = [
        {"html_url": url, "body": "github body"},
        {"web_url": "", "html_url": None, "url": 7, "description": None, "body": None},
    ]

    assert list(open_candidates(host=host, repo="acme/widgets")) == [url]
    assert description_of({"description": None, "body": None}) == ""
    assert description_of({"body": "github body"}) == "github body"
    assert issue_url_of({"web_url": "", "html_url": None, "url": 7}) == ""
    assert list(select_closest({url: {"title": None, "body": None}}, "", "")) == [url]
