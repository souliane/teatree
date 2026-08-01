"""Work-group grouping — transitive on any shared signal, inert on a generic scope."""

import pytest

from teatree.core.review.work_group import GroupSignal, SignalKind, group_members, signals_for

GENERIC_SCOPES = frozenset({"chore", "ci", "docs"})

TICKET_URL = "https://forge.example/some-namespace/product/-/issues/42"
OTHER_TICKET_URL = "https://forge.example/some-namespace/gateway/-/work_items/7"
PRODUCT_MR = "https://forge.example/some-namespace/product/-/merge_requests/11"
GATEWAY_MR = "https://forge.example/some-namespace/gateway/-/merge_requests/3"


def _partition(items: list[tuple[str, str]]) -> set[frozenset[str]]:
    return set(group_members(items, generic_scopes=GENERIC_SCOPES).values())


class TestSignalExtraction:
    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            (f"feat: rates ({TICKET_URL})", {GroupSignal(SignalKind.TICKET, TICKET_URL)}),
            (f"fix: rates ({OTHER_TICKET_URL})", {GroupSignal(SignalKind.TICKET, OTHER_TICKET_URL)}),
            ("feat: rates (product#12)", {GroupSignal(SignalKind.TICKET, "product#12")}),
            ("feat: rates (some-namespace/product#12)", {GroupSignal(SignalKind.TICKET, "some-namespace/product#12")}),
            ("feat: rates for issue #12", set()),
            ("feat: rates [core_tenant_market_rates]", {GroupSignal(SignalKind.FLAG, "core_tenant_market_rates")}),
            ("feat(alpha)!: breaking rates", {GroupSignal(SignalKind.SCOPE, "alpha")}),
            ("feat(ALPHA): rates", {GroupSignal(SignalKind.SCOPE, "alpha")}),
            ("feat(ci): pipeline", set()),
            ("feat(): rates", set()),
            ("feat: rates", set()),
            ("rates went up", set()),
        ],
    )
    def test_narrow_extraction(self, title: str, expected: set[GroupSignal]) -> None:
        assert signals_for(title, generic_scopes=GENERIC_SCOPES) == expected

    @pytest.mark.parametrize("literal", ["none", "NONE", "Aikido", "aikido"])
    def test_placeholder_and_scanner_brackets_are_not_flags(self, literal: str) -> None:
        assert signals_for(f"fix: patch [{literal}]", generic_scopes=GENERIC_SCOPES) == set()

    def test_a_title_can_carry_every_kind_at_once(self) -> None:
        title = f"feat(alpha): rates [tenant_rates] ({TICKET_URL})"
        assert signals_for(title, generic_scopes=GENERIC_SCOPES) == {
            GroupSignal(SignalKind.TICKET, TICKET_URL),
            GroupSignal(SignalKind.FLAG, "tenant_rates"),
            GroupSignal(SignalKind.SCOPE, "alpha"),
        }


class TestGrouping:
    @pytest.mark.parametrize(
        ("case", "items", "expected"),
        [
            (
                "shared ticket url",
                [("mr/1", f"feat(alpha): a ({TICKET_URL})"), ("mr/2", f"fix(beta): b ({TICKET_URL})")],
                {frozenset({"mr/1", "mr/2"})},
            ),
            (
                "shared ticket ref",
                [("mr/1", "feat(alpha): a (product#12)"), ("mr/2", "fix(beta): b (product#12)")],
                {frozenset({"mr/1", "mr/2"})},
            ),
            (
                "shared flag",
                [("mr/1", "feat(alpha): a [tenant_rates]"), ("mr/2", "fix(beta): b [tenant_rates]")],
                {frozenset({"mr/1", "mr/2"})},
            ),
            (
                "shared non-generic scope",
                [("mr/1", "feat(alpha): a"), ("mr/2", "fix(alpha): b")],
                {frozenset({"mr/1", "mr/2"})},
            ),
            (
                "shared generic scope only",
                [("mr/1", "feat(ci): a"), ("mr/2", "fix(ci): b")],
                {frozenset({"mr/1"}), frozenset({"mr/2"})},
            ),
            (
                "distinct tickets stay apart",
                [("mr/1", f"feat(alpha): a ({TICKET_URL})"), ("mr/2", f"fix(beta): b ({OTHER_TICKET_URL})")],
                {frozenset({"mr/1"}), frozenset({"mr/2"})},
            ),
            (
                "transitive across signal kinds",
                [
                    ("mr/a", f"feat(alpha): a ({TICKET_URL})"),
                    ("mr/b", f"fix(beta): b ({TICKET_URL}) [tenant_rates]"),
                    ("mr/c", "improvement(gamma): c [tenant_rates]"),
                ],
                {frozenset({"mr/a", "mr/b", "mr/c"})},
            ),
            (
                "signal-less merge request is a group of itself",
                [("mr/1", "bump the pinned toolchain"), ("mr/2", "tidy the changelog")],
                {frozenset({"mr/1"}), frozenset({"mr/2"})},
            ),
            (
                "cross-repo, one ticket",
                [(PRODUCT_MR, f"feat(alpha): a ({TICKET_URL})"), (GATEWAY_MR, f"fix(beta): b ({TICKET_URL})")],
                {frozenset({PRODUCT_MR, GATEWAY_MR})},
            ),
            (
                "placeholder bracket never fuses",
                [("mr/1", "feat(alpha): a [none]"), ("mr/2", "fix(beta): b [none]")],
                {frozenset({"mr/1"}), frozenset({"mr/2"})},
            ),
            (
                "scanner bracket never fuses",
                [("mr/1", "fix(alpha): a [Aikido]"), ("mr/2", "fix(beta): b [Aikido]")],
                {frozenset({"mr/1"}), frozenset({"mr/2"})},
            ),
            ("no merge requests at all", [], set()),
        ],
    )
    def test_partition(self, case: str, items: list[tuple[str, str]], expected: set[frozenset[str]]) -> None:
        assert _partition(items) == expected, case

    def test_every_member_maps_to_the_same_group(self) -> None:
        items = [
            ("mr/a", f"feat(alpha): a ({TICKET_URL})"),
            ("mr/b", f"fix(beta): b ({TICKET_URL}) [tenant_rates]"),
            ("mr/c", "improvement(gamma): c [tenant_rates]"),
        ]
        groups = group_members(items, generic_scopes=GENERIC_SCOPES)
        whole = frozenset({"mr/a", "mr/b", "mr/c"})
        assert groups == {"mr/a": whole, "mr/b": whole, "mr/c": whole}

    def test_a_generic_scope_does_not_widen_a_real_group(self) -> None:
        items = [
            ("mr/1", f"chore(ci): a ({TICKET_URL})"),
            ("mr/2", f"chore(ci): b ({TICKET_URL})"),
            ("mr/3", "chore(ci): unrelated pipeline tidy"),
        ]
        assert _partition(items) == {frozenset({"mr/1", "mr/2"}), frozenset({"mr/3"})}

    def test_an_empty_generic_scope_set_lets_every_scope_group(self) -> None:
        items = [("mr/1", "feat(ci): a"), ("mr/2", "fix(ci): b")]
        assert set(group_members(items, generic_scopes=frozenset()).values()) == {frozenset({"mr/1", "mr/2"})}

    def test_a_repeated_url_collapses_rather_than_duplicating(self) -> None:
        items = [("mr/1", "feat(alpha): a"), ("mr/1", "feat(alpha): a")]
        assert group_members(items, generic_scopes=GENERIC_SCOPES) == {"mr/1": frozenset({"mr/1"})}
