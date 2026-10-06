import logging

import pytest

from teatree.core.review.gitlab_head_pipeline import select_head_pipeline

_TRAIN = {"id": 3, "status": "canceled", "sha": "c" * 40, "source": "merge_train"}
_OLDER = {"id": 2, "status": "success", "sha": "b" * 40, "source": "push"}


def test_an_unnamed_head_falls_back_to_the_newest_non_train_pipeline() -> None:
    assert select_head_pipeline([_TRAIN, _OLDER], "", slug="org/repo", pr_id=1) == _OLDER


def test_a_named_head_with_only_train_and_older_pipelines_has_no_pipeline_of_its_own() -> None:
    assert select_head_pipeline([_TRAIN, _OLDER], "a" * 40, slug="org/repo", pr_id=1) is None


def test_an_unmatched_head_is_reported_once_however_often_it_is_listed(caplog: pytest.LogCaptureFixture) -> None:
    head = "d" * 40
    with caplog.at_level(logging.INFO, logger="teatree.core.review.gitlab_head_pipeline"):
        for _ in range(3):
            select_head_pipeline([_OLDER], head, slug="org/repo", pr_id=9)

    assert [r.levelno for r in caplog.records if head in r.getMessage()] == [logging.INFO]
