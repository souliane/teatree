"""Canonical default loops + prompts, and their idempotent seed (#2513).

The single source of truth for which autonomous :class:`Loop` rows ship by
default and how each is invoked (its on-disk ``script`` or its reusable
:class:`Prompt`). Migrations seeded these at migrate-time; this module is the
install-time seed ``t3 setup`` runs so a fresh — or squashed-migration — install
has them present regardless of migration history.

The seed is **idempotent**: it ``get_or_create``s by ``name``, so re-running it
creates nothing new and NEVER clobbers an operator-edited row (a disabled loop,
a re-pointed cadence). Each script-backed loop points at its OWN on-disk module
``src/teatree/loops/<name>/loop.py`` (the file exposing that loop's ``MINI_LOOP``)
— the ``script`` column is PER-LOOP and load-bearing, never a value shared across
rows (the loop XOR: exactly one of script/prompt). ``arch_review`` is the one
prompt-backed default; its prompt instructs a sub-agent to run an architectural
review using the ``ac-reviewing-codebase`` skill.

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

#: The architectural-review prompt body — a real instruction telling the
#: sub-agent to run an architectural review using the ``ac-reviewing-codebase``
#: skill (owner's explicit decision). Shared with the data migration so the
#: install-seed and the migrate-time seed agree.
ARCH_REVIEW_PROMPT_BODY = (
    "Run an architectural review of the codebase using the ac-reviewing-codebase skill. "
    "Dispatch a sub-agent that loads /ac-reviewing-codebase and performs a holistic, "
    "codebase-wide architectural review, surfacing findings as the skill prescribes."
)


def script_entry_point_for(name: str) -> str:
    """The per-loop on-disk module a script-backed loop named *name* points at.

    Each script loop's ``script`` is its OWN module — never a value shared across
    rows. This is the single place the canonical ``src/teatree/loops/<name>/loop.py``
    shape is built.
    """
    return f"src/teatree/loops/{name}/loop.py"


@dataclass(frozen=True, slots=True)
class LoopSeedSpec:
    """One default loop's seed config — name, cadence, and how it is invoked.

    ``prompt_body`` set ⇒ a prompt-backed loop (a :class:`Prompt` named for the
    loop is seeded and the FK points at it); otherwise the loop is script-backed
    at its OWN module (:func:`script_entry_point_for`). ``daily_at`` overrides the
    interval for a once-per-day loop.
    """

    name: str
    delay_seconds: int
    daily_at: dt.time | None = None
    prompt_body: str | None = None

    @property
    def is_prompt_backed(self) -> bool:
        return self.prompt_body is not None

    @property
    def script_entry_point(self) -> str:
        """This loop's OWN on-disk module ``src/teatree/loops/<name>/loop.py``."""
        return script_entry_point_for(self.name)


# One autonomous loop per row, each on its own cadence. Every script-backed loop
# points at its OWN module; ``arch_review`` is the single prompt-backed default.
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
    LoopSeedSpec("arch_review", 10800, prompt_body=ARCH_REVIEW_PROMPT_BODY),
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
            defaults["script"] = spec.script_entry_point
        _, made = Loop.objects.get_or_create(name=spec.name, defaults=defaults)
        loops_created += int(made)
    return SeedResult(loops_created=loops_created, prompts_created=prompts_created)
