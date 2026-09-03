"""The project-board read: label completeness and the board-order walk.

A label the board read drops is invisible to every consumer downstream — the
ticket-kind classifier and the agent prompt both read ``ProjectItem.labels`` —
so the page size and the truncation signal are the contract under test.
"""

import json
import logging
import re
import subprocess

import pytest

from teatree.backends.github.projects import fetch_project_items

_LABEL_PAGE_SIZE_IN_QUERY = re.compile(r"labels\(first: (\d+)\)")


def _node(number: int, labels: list[str], page_size: int) -> dict[str, object]:
    """One board node as GitHub answers it: labels TRUNCATED to the requested page."""
    return {
        "fieldValueByName": {"name": "Todo"},
        "content": {
            "number": number,
            "title": f"issue {number}",
            "url": f"https://github.com/acme/repo/issues/{number}",
            "updatedAt": "2026-01-01T00:00:00Z",
            "labels": {
                "totalCount": len(labels),
                "nodes": [{"name": name} for name in labels[:page_size]],
            },
        },
    }


def _install(monkeypatch: pytest.MonkeyPatch, labels: list[str]) -> None:
    def fake_run_checked(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        match = _LABEL_PAGE_SIZE_IN_QUERY.search(argv[-1])
        assert match is not None, "the board query must request a labels page"
        body = {
            "data": {
                "user": {
                    "projectV2": {
                        "items": {
                            "pageInfo": {"hasNextPage": False, "endCursor": ""},
                            "nodes": [_node(7, labels, int(match.group(1)))],
                        },
                    }
                }
            }
        }
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(body), stderr="")

    monkeypatch.setattr("teatree.backends.github.projects.run_checked", fake_run_checked)


class TestLabelCompleteness:
    def test_a_label_past_the_first_ten_still_reaches_the_item(self, monkeypatch: pytest.MonkeyPatch) -> None:
        labels = [f"area/{i}" for i in range(12)] + ["needs-triage"]
        _install(monkeypatch, labels)

        [item] = fetch_project_items("acme", 3)

        assert item.labels == labels

    def test_a_label_set_larger_than_one_page_is_logged_not_silently_dropped(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _install(monkeypatch, [f"area/{i}" for i in range(101)])

        with caplog.at_level(logging.WARNING):
            fetch_project_items("acme", 3)

        assert "carries 101 labels" in caplog.text
