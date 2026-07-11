"""The canonical :class:`PrRef` value object (slug, pr_id, host_kind).

One frozen ``(slug, pr_id, host_kind)`` triple shared by the URL parser, the forge
classifier, and the merge chokepoint's ``CodeHostQuery``. These tests pin the
value-object contract (frozen, slotted, value-equal, ``github`` default) and that
``pr_ref_from_url`` returns exactly this class with the ``pr_id`` field — the
promotion that let the merge layer drop the ``slug, pr_id, *, host_kind`` triple.
"""

import dataclasses

import pytest

from teatree.utils.pr_ref import PrRef
from teatree.utils.url_slug import pr_ref_from_url


class TestPrRefValueObject:
    def test_host_kind_defaults_to_github(self) -> None:
        assert PrRef(slug="owner/repo", pr_id=7).host_kind == "github"

    def test_is_frozen(self) -> None:
        ref = PrRef(slug="owner/repo", pr_id=7)
        with pytest.raises(dataclasses.FrozenInstanceError):
            ref.pr_id = 8  # type: ignore[misc]

    def test_is_slotted_no_instance_dict(self) -> None:
        # ``slots=True`` — the object carries no ``__dict__``, so a typo'd attribute
        # cannot be silently attached (it would mask a real field).
        assert not hasattr(PrRef(slug="owner/repo", pr_id=7), "__dict__")

    def test_value_equality(self) -> None:
        assert PrRef(slug="o/r", pr_id=1, host_kind="gitlab") == PrRef(slug="o/r", pr_id=1, host_kind="gitlab")
        assert PrRef(slug="o/r", pr_id=1) != PrRef(slug="o/r", pr_id=2)


class TestPrRefFromUrlReturnsCanonical:
    def test_github_pull_url_carries_pr_id(self) -> None:
        ref = pr_ref_from_url("https://github.com/souliane/teatree/pull/1680")
        assert ref == PrRef(slug="souliane/teatree", pr_id=1680, host_kind="github")
        assert ref is not None
        assert ref.pr_id == 1680

    def test_gitlab_mr_url_carries_pr_id_and_gitlab_transport(self) -> None:
        ref = pr_ref_from_url("https://gitlab.com/group/sub/project/-/merge_requests/42")
        assert ref == PrRef(slug="group/sub/project", pr_id=42, host_kind="gitlab")

    def test_unrecognised_url_is_none(self) -> None:
        assert pr_ref_from_url("https://github.com/souliane/teatree/issues/1680") is None
