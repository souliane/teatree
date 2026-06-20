"""Canonical default loops + prompts, and their idempotent seed (#2513).

The single source of truth for which autonomous :class:`Loop` rows ship by
default and how each is invoked (its on-disk ``script`` or its reusable
:class:`Prompt`). Migrations 0078/0080/0085 seeded these at migrate-time; this
module is the install-time seed ``t3 setup`` runs so a fresh — or
squashed-migration — install has them present regardless of migration history.

The seed is **idempotent**: it ``get_or_create``s by ``name``, so re-running it
creates nothing new and NEVER clobbers an operator-edited row (a disabled loop,
a re-pointed cadence). The script-backed loops point at the shared per-loop
runner ``src/teatree/loops/run.py`` (the loop XOR: exactly one of script/prompt);
``arch_review`` is the one prompt-backed default.

**No orphan rows (#2584).** Every name in :data:`DEFAULT_LOOPS` has a registry
``MiniLoop`` (a ``teatree.loops.<name>.loop`` package exposing ``MINI_LOOP``), so
the seeded ``Loop``-table set equals :func:`teatree.loops.registry.iter_loops`.
``slack_answer`` is intentionally NOT a default Loop row: it has no registry
``MiniLoop`` — the autonomous ``build_loop_table_jobs`` / ``iter_loops`` fan-out
can never run it. It runs ONLY via the won-tick piggyback cycle
(:func:`teatree.loop.tick_piggyback.run_piggyback_cycles` →
``teatree.loop.slack_answer.cycle.run_slack_answer_cycle``), behind its own
``loop-slack-answer`` lease. Seeding a ``slack_answer`` Loop row would create an
orphan the master tick can never fan out (the 19-vs-18 seed/registry mismatch
this module's parity test pins).
"""

import datetime as dt
from dataclasses import dataclass

_SCRIPT_ENTRY_POINT = "src/teatree/loops/run.py"


@dataclass(frozen=True, slots=True)
class LoopSeedSpec:
    """One default loop's seed config — name, cadence, and how it is invoked.

    ``prompt_body`` set ⇒ a prompt-backed loop (a :class:`Prompt` named for the
    loop is seeded and the FK points at it); otherwise the loop is script-backed
    at :data:`_SCRIPT_ENTRY_POINT`. ``daily_at`` overrides the interval for a
    once-per-day loop.
    """

    name: str
    delay_seconds: int
    daily_at: dt.time | None = None
    prompt_body: str | None = None

    @property
    def is_prompt_backed(self) -> bool:
        return self.prompt_body is not None


# Mirror of migrations 0078/0080: one autonomous loop per row, each on its own
# cadence. ``arch_review`` is the single prompt-backed default; the rest are
# script-backed at the shared per-loop runner.
DEFAULT_LOOPS: tuple[LoopSeedSpec, ...] = (
    LoopSeedSpec("inbox", 60),
    LoopSeedSpec("idle_stack_reaper", 60),
    LoopSeedSpec("local_stack_queue", 60),
    LoopSeedSpec("resource_pressure", 60),
    # NOTE: ``slack_answer`` is intentionally absent — it has no registry
    # MiniLoop and runs only via the won-tick piggyback cycle (see the module
    # docstring). A seeded row would be an orphan the master tick can never run.
    LoopSeedSpec("dispatch", 300),
    LoopSeedSpec("tickets", 300),
    LoopSeedSpec("review", 300),
    LoopSeedSpec("ship", 300),
    LoopSeedSpec("pane_reaper", 300),
    LoopSeedSpec("audit", 1800),
    LoopSeedSpec("followup", 1800),
    LoopSeedSpec("issue_implementer", 3600),
    LoopSeedSpec("housekeeping", 3600),
    LoopSeedSpec("arch_review", 10800, prompt_body="Run a sub-agent to run the arch_review loop."),
    LoopSeedSpec("dogfood", 86400),
    LoopSeedSpec("eval_local", 86400),
    LoopSeedSpec("news", 86400, daily_at=dt.time(8, 0)),
    LoopSeedSpec("dream", 86400, daily_at=dt.time(3, 0)),
)


@dataclass(frozen=True, slots=True)
class SeedResult:
    """How many rows the seed created (existing rows are untouched)."""

    loops_created: int
    prompts_created: int


def seed_default_loops_and_prompts() -> SeedResult:
    """Idempotently seed the default loops + prompts; return the create counts.

    ``get_or_create`` by ``name`` so an existing operator-edited row is left
    exactly as-is — the seed only fills in rows that are absent. A prompt-backed
    loop's :class:`Prompt` is seeded first so the FK resolves.

    **Seeded paused (#2513 cutover).** Every default loop lands ``enabled=False``.
    The cutover is plumbing only — no loop ticks until an operator deliberately
    enables it. ``get_or_create`` never reaches the ``defaults`` for a row that
    already exists, so an operator who has since ENABLED a loop keeps that choice.
    """
    from teatree.core.models import Loop, Prompt  # noqa: PLC0415

    loops_created = 0
    prompts_created = 0
    for spec in DEFAULT_LOOPS:
        prompt = None
        if spec.is_prompt_backed:
            prompt, made = Prompt.objects.get_or_create(
                name=spec.name,
                defaults={"body": spec.prompt_body or "", "description": f"Default loop prompt for {spec.name!r}."},
            )
            prompts_created += int(made)
        defaults = {
            "delay_seconds": spec.delay_seconds,
            "daily_at": spec.daily_at,
            "enabled": False,
        }
        if prompt is not None:
            defaults["prompt"] = prompt
        else:
            defaults["script"] = _SCRIPT_ENTRY_POINT
        _, made = Loop.objects.get_or_create(name=spec.name, defaults=defaults)
        loops_created += int(made)
    return SeedResult(loops_created=loops_created, prompts_created=prompts_created)
