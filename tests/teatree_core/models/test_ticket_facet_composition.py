"""Composition guards for the ``Ticket`` facet chain (burndown P4 nits).

Covers two structural properties that ``test_ticket_structure.py`` (public API,
FSM graph, method count) does not.

First: the concrete ``Ticket`` does not redundantly re-list ``models.Model`` as a
direct base — every facet already derives from it via ``TicketFacet`` — while
still resolving to a concrete Django model.

Second: the former grab-bag ``TicketStatusModel`` (identity / liveness / diff
introspection AND the small direct-write status flags) is split along its own
read/write seam — read-only introspection on ``TicketIntrospectionModel``, the
direct-write status-flag mutations on ``TicketStatusModel``. Both stay abstract,
so no field moves and no migration is produced.
"""

import inspect

from django.db import models

from teatree.core.models.ticket import Ticket
from teatree.core.models.ticket_introspection import TicketIntrospectionModel
from teatree.core.models.ticket_status import TicketStatusModel


def _own_public_members(cls: type) -> set[str]:
    """Public methods/properties defined directly in *cls*'s body."""
    members: set[str] = set()
    for name, value in vars(cls).items():
        if name.startswith("_"):
            continue
        if isinstance(value, (staticmethod, classmethod, property)) or inspect.isfunction(value):
            members.add(name)
    return members


class TestNoRedundantModelBase:
    def test_models_model_is_not_a_direct_base(self) -> None:
        # The facets already carry ``models.Model`` through ``TicketFacet``;
        # re-listing it on ``Ticket`` is redundant. Dropping it must not change
        # the fact that ``Ticket`` is a concrete Django model.
        assert models.Model not in Ticket.__bases__

    def test_ticket_remains_a_concrete_django_model(self) -> None:
        assert issubclass(Ticket, models.Model)
        assert Ticket._meta.abstract is False


class TestStatusFacetCohesionSplit:
    def test_status_facet_holds_only_direct_write_flags(self) -> None:
        assert _own_public_members(TicketStatusModel) == {"mark_remote_missing", "unignore"}

    def test_introspection_facet_holds_the_read_only_surface(self) -> None:
        assert _own_public_members(TicketIntrospectionModel) == {
            "has_active_work",
            "is_terminal",
            "may_expedite",
            "ticket_number",
            "has_shippable_diff",
            "artifacts",
        }

    def test_both_facets_stay_abstract_so_no_field_moves(self) -> None:
        assert TicketStatusModel._meta.abstract is True
        assert TicketIntrospectionModel._meta.abstract is True

    def test_ticket_composes_both_facets(self) -> None:
        assert issubclass(Ticket, TicketStatusModel)
        assert issubclass(Ticket, TicketIntrospectionModel)
