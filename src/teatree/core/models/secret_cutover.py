"""Durable marker for the one-shot password-store cutover."""

from typing import ClassVar

from django.db import models


class SecretCutover(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    applied_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "teatree_secret_cutover"
        constraints: ClassVar = [models.CheckConstraint(condition=models.Q(id=1), name="secret_cutover_singleton")]

    def __str__(self) -> str:
        return "secret-cutover<1>"
