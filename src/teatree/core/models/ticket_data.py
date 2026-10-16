from typing import TYPE_CHECKING, ClassVar

from django.db import models

if TYPE_CHECKING:
    from datetime import datetime

    from teatree.core.models.ticket import Ticket


class TicketFacet(models.Model):
    """Field-less abstract base carrying the type surface the ``Ticket`` facets share.

    The concrete ``Ticket`` supplies the real fields/enums; each behaviour facet
    (overlay attribution, phase sessions, scheduling, evidence, introspection)
    subclasses this so its methods type-check against the model's fields without
    redeclaring them. Abstract with no fields, so it contributes no migration and
    the multiple-inheritance diamond into ``Ticket`` cannot clash. The fields are
    declared as never-executed field instances so ``Ticket``'s own fields match them.
    """

    class Meta:
        abstract = True

    if TYPE_CHECKING:
        pk: int
        issue_url = models.URLField()
        overlay = models.CharField()
        state = models.CharField()
        role = models.CharField()
        extra = models.JSONField()
        context = models.TextField()
        short_description = models.CharField()
        remote_missing = models.BooleanField()
        expedited = models.BooleanField()
        issue_number = models.CharField()
        # Present only on rows from ``TicketQuerySet.unfindable()``, which annotates it;
        # ``Ticket`` carries no creation stamp, so its oldest task IS the row's age.
        oldest_task: "datetime | None"
        # Present only on rows whose queryset annotates the newest task's stamp or pk.
        newest_task: "datetime | None"
        newest_task_pk: "int | None"
        State: type["Ticket.State"]
        Role: type["Ticket.Role"]
        _SETTLED_STATES: ClassVar[frozenset[str]]
        _WORK_STATE_ORDER: ClassVar[tuple[str, ...]]
        _PHASE_PRODUCES_STATE: ClassVar[dict[str, str]]

        # The two ladder transitions a facet drives; the concrete ``Ticket`` declares
        # them with ``@transition``, which a static checker cannot see through.
        def scope(self) -> None: ...
        def start(self) -> None: ...
