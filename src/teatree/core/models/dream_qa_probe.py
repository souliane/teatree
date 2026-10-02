"""Retention/interference/monotonicity QA corpus for the dream engine (#1933).

A :class:`DreamQaProbe` is one question/expected-answer pair the dreaming
engine replays to detect memory regressions across consolidation runs —
retention (a fact still recalled), interference (a new rule did not corrupt
an old answer), and monotonicity (the pass rate does not regress over runs).
The corpus is persisted so probes survive across runs: a prior-session
probe (``is_prior_session``) checks that consolidation did not forget
something learned in an earlier session.

``probe_key`` (sha256 of the scope + NUL byte + question) is the unique
idempotency anchor — re-recording the same question in the same scope finds the
existing row and accumulates its pass/run counts rather than spawning a duplicate. The
scope is folded into the key so two memory dirs holding a same-named memory
(hence the same question) get DISTINCT rows instead of colliding on one.

Mirrors :class:`teatree.core.models.consolidated_memory.ConsolidatedMemory`
(idempotent sha256 key + custom manager).
"""

from typing import ClassVar

from django.db import models


class DreamQaProbeManager(models.Manager["DreamQaProbe"]):
    """Read surface for the dream engine's QA replay."""

    def prior_session_probes(self, scope: str) -> "models.QuerySet[DreamQaProbe]":
        """Probes carried over from an earlier session in *scope* — the retention corpus."""
        return self.filter(scope=scope, is_prior_session=True)

    def current_corpus(self, scope: str) -> "models.QuerySet[DreamQaProbe]":
        """Every probe recorded for *scope*."""
        return self.filter(scope=scope)


class DreamQaProbe(models.Model):
    """One question/expected-answer probe in the dream QA corpus.

    ``probe_key`` (sha256 of the scope + NUL byte + question) is unique so the same
    question re-recorded in the same scope accumulates onto one row.
    ``last_pass_rate`` is recomputed from ``pass_count`` / ``run_count`` on every
    recorded result. ``scope`` is the corpus partition (the memory dir the probe
    came from) so per-dir baselines never bleed together.
    """

    probe_key = models.CharField(max_length=64, unique=True)
    question = models.TextField()
    expected_answer = models.TextField()
    source_memory_path = models.CharField(max_length=512, blank=True, default="")
    scope = models.CharField(max_length=512, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    last_pass_rate = models.FloatField(default=0.0)
    pass_count = models.PositiveIntegerField(default=0)
    run_count = models.PositiveIntegerField(default=0)
    is_prior_session = models.BooleanField(default=False)

    objects: ClassVar[DreamQaProbeManager] = DreamQaProbeManager()

    class Meta:
        db_table = "teatree_dream_qa_probe"
        ordering: ClassVar = ["-created_at"]

    def __str__(self) -> str:
        return f"dream-qa-probe<{self.pk}:{self.last_pass_rate:.2f}:{self.question[:40]}>"

    def record_result(self, *, passed: bool) -> None:
        """Record one replay outcome and recompute the running pass rate."""
        self.run_count += 1
        if passed:
            self.pass_count += 1
        self.last_pass_rate = self.pass_count / self.run_count
        self.save(update_fields=["run_count", "pass_count", "last_pass_rate"])
