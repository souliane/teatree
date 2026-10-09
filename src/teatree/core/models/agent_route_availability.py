from django.db import models


class AgentRouteAvailabilityManager(models.Manager["AgentRouteAvailability"]):
    def forget_harness(self, harness: str) -> int:
        """Drop every observation of *harness*: its credential was replaced, so none of its holds stand."""
        deleted, _ = self.filter(harness=harness).delete()
        return deleted


class AgentRouteAvailability(models.Model):
    overlay = models.CharField(max_length=128, blank=True, default="")
    harness = models.CharField(max_length=128)
    provider = models.CharField(max_length=128, blank=True, default="")
    model = models.CharField(max_length=255)
    phase = models.CharField(max_length=128, blank=True, default="")
    unavailable_reason = models.TextField(blank=True, default="")
    observed_at = models.DateTimeField()
    retry_at = models.DateTimeField()

    objects = AgentRouteAvailabilityManager()

    class Meta:
        db_table = "teatree_agentrouteavailability"
        verbose_name_plural = "agent route availabilities"
        constraints = (
            models.UniqueConstraint(
                fields=("overlay", "harness", "provider", "model", "phase"),
                name="uniq_agent_route_availability",
            ),
        )

    def __str__(self) -> str:
        return f"{self.overlay}:{self.harness}:{self.provider}:{self.model}:{self.phase}"
