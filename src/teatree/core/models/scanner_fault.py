"""Durable last failure for each scanner job identity."""

import datetime as dt
from typing import ClassVar

from django.db import models
from django.utils import timezone


class ScannerFaultManager(models.Manager["ScannerFault"]):
    def record_failure(self, scanner: str, error_class: str, detail: str) -> None:
        self.update_or_create(
            scanner=scanner,
            defaults={
                "error_class": error_class,
                "detail": detail[:200],
                "last_failed_at": timezone.now(),
                "healed_at": None,
            },
        )

    def heal(self, scanner: str) -> None:
        open_fault = self.filter(scanner=scanner, healed_at__isnull=True)
        if open_fault.exists():
            open_fault.update(healed_at=timezone.now())

    def recent_open(self, cutoff: dt.datetime) -> models.QuerySet["ScannerFault"]:
        return self.filter(healed_at__isnull=True, last_failed_at__gt=cutoff).order_by("scanner")


class ScannerFault(models.Model):
    scanner = models.CharField(max_length=255, unique=True)
    error_class = models.CharField(max_length=128)
    detail = models.CharField(max_length=200, blank=True)
    last_failed_at = models.DateTimeField(default=timezone.now)
    healed_at = models.DateTimeField(null=True, blank=True)

    objects: ClassVar[ScannerFaultManager] = ScannerFaultManager()

    class Meta:
        db_table = "teatree_scanner_fault"

    def __str__(self) -> str:
        return f"scanner-fault<{self.scanner}>"
