"""The earliest admission timestamp across the durable clock and the renewable seat."""

from django.db import models
from django.db.models.functions import Coalesce, Least


def earliest_admission_stamp() -> models.Expression:
    return Coalesce(
        Least("first_admitted_at", "admitted_at"),
        "first_admitted_at",
        "admitted_at",
        output_field=models.DateTimeField(),
    )
