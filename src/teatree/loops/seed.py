"""Canonical default loops + prompts, and their idempotent seed (#2513).

Which autonomous :class:`Loop` rows ship by default — and how each is invoked (its
on-disk ``script`` or its reusable :class:`Prompt`) — is SHIPPED DATA, not code: the
values live in the ``[loops.<name>]`` tables of ``src/teatree/config/defaults.toml``
alongside every other shipped default an operator tunes, and this module builds the
:class:`LoopSeedSpec` set from them. Migrations seeded these at migrate-time; this
module is the install-time seed ``t3 setup`` runs so a fresh — or squashed-migration —
install has them present regardless of migration history.

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
The reactive infra loops (``slack_answer``, ``self_improve``, ``drain_queue``)
are intentionally NOT default Loop rows: they have no registry ``MiniLoop`` — the
per-loop ``build_loop_table_jobs`` / ``iter_loops`` fan-out can never run them.
Each runs as its OWN dedicated native Claude ``/loop`` firing its own
``t3 loop <slot> run`` command (``teatree.cli.loop*``), behind its own dedicated
``LoopLease`` (``loop-slack-answer`` / ``loop-self-improve`` / ``loop-drain-queue``).
Seeding one as a ``Loop`` row would create an orphan a per-loop tick could never
fan out (the seed/registry parity this module's test pins).
"""

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from teatree.config.seed_defaults import shipped_seed_table

_LOOPS_TABLE = "loops"


def script_entry_point_for(name: str) -> str:
    """The per-loop on-disk module a script-backed loop named *name* points at.

    Each script loop's ``script`` is its OWN module — never a value shared across
    rows. This is the single place the canonical ``src/teatree/loops/<name>/loop.py``
    shape is built.
    """
    return f"src/teatree/loops/{name}/loop.py"


@dataclass(frozen=True, slots=True)
class LoopSeedSpec:
    """One default loop's seed config — name, cadence, description, and how it is invoked.

    ``prompt_body`` set ⇒ a prompt-backed loop (a :class:`Prompt` named for the
    loop is seeded and the FK points at it); otherwise the loop is script-backed
    at its OWN module (:func:`script_entry_point_for`). ``daily_at`` overrides the
    interval for a once-per-day loop. ``description`` is the loop's real one-line
    "what it does and when" — the source of truth populated onto ``Loop.description``
    (and the prompt-backed loop's ``Prompt.description``) and rendered by
    ``t3 loops list``. ``colleague_facing`` (#2904) is the AWAY-GATE policy —
    the #2904 admission gate skips the loop whenever availability defers questions
    (the owner is unreachable). It is operator-editable and narrower than the
    ``colleague`` reach tag every loop declares in code (#3959): ``review`` reaches
    colleagues and deliberately keeps running while the owner is away. ``default_enabled``
    ships the local/read-only operational core ON out of the box (the sound
    default the squashed ``0001_initial`` seeds ``enabled=True`` on a fresh DB);
    every colleague-facing, externally-visible, destructive-capable, or
    token-costly loop stays ``False`` (opt-in).
    """

    name: str
    delay_seconds: int
    description: str
    daily_at: dt.time | None = None
    prompt_body: str | None = None
    colleague_facing: bool = False
    default_enabled: bool = False

    @property
    def is_prompt_backed(self) -> bool:
        return self.prompt_body is not None

    @property
    def script_entry_point(self) -> str:
        """This loop's OWN on-disk module ``src/teatree/loops/<name>/loop.py``."""
        return script_entry_point_for(self.name)


def load_loop_specs(path: Path | None = None) -> tuple[LoopSeedSpec, ...]:
    """The shipped ``[loops]`` table as specs, in the file's table order.

    File order IS seed order: the frozen ``0001_initial`` copy is pinned against this
    tuple, so a reordering of the tables is a reviewed change, not a formatting whim.
    An omitted optional field falls back to the dataclass default, so a loop that is
    neither prompt-backed nor daily-scheduled needs only its cadence and description.
    """
    return tuple(
        LoopSeedSpec(
            name=name,
            delay_seconds=entry["delay_seconds"],
            description=entry["description"],
            daily_at=entry.get("daily_at"),
            prompt_body=entry.get("prompt_body"),
            colleague_facing=entry.get("colleague_facing", False),
            default_enabled=entry.get("default_enabled", False),
        )
        for name, entry in shipped_seed_table(_LOOPS_TABLE, path).items()
    )


#: One autonomous loop per row, each on its own cadence. Every script-backed loop
#: points at its OWN module; ``arch_review`` is the single prompt-backed default.
DEFAULT_LOOPS: tuple[LoopSeedSpec, ...] = load_loop_specs()

#: The architectural-review prompt body — a real instruction telling the sub-agent to run
#: an architectural review using the ``ac-reviewing-codebase`` skill (owner's explicit
#: decision). Shared with the data migration so the install-seed and the migrate-time seed
#: agree; shipped in ``[loops.arch_review] prompt_body``.
ARCH_REVIEW_PROMPT_BODY: str = next(spec.prompt_body or "" for spec in DEFAULT_LOOPS if spec.is_prompt_backed)


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

    **Sound operational defaults (reversing the #2513 all-paused cutover).** The
    local/read-only operational core (``spec.default_enabled``) lands
    ``enabled=True`` so a fresh install works out of the box; every
    colleague-facing, externally-visible, destructive-capable, or token-costly
    loop stays ``enabled=False`` (opt-in). ``get_or_create`` never reaches the
    ``defaults`` for a row that already exists, so an operator who ENABLED a
    paused loop — or DISABLED a default-on one — keeps that choice.

    **Descriptions backfill onto existing rows.** ``get_or_create`` populates
    ``description`` on a fresh row; an earlier install's row predates the field and
    carries a blank one, so the seed also backfills any blank ``description`` from
    the spec. The backfill filters on ``description=""``, so it is idempotent and
    never clobbers a description an operator rewrote.

    ``colleague_facing`` is admin-editable, so the seed leaves it alone; a row that
    disagrees with the shipped table is surfaced by
    :func:`teatree.loops.seed_drift.classification_drift` and reconciled only on the
    explicit ``seed_loops --reconcile-classification`` run.
    """
    from teatree.core.models import Loop, Prompt  # noqa: PLC0415 — deferred: ORM import needs the app registry

    loops_created = 0
    prompts_created = 0
    for spec in DEFAULT_LOOPS:
        prompt = None
        if spec.is_prompt_backed:
            prompt, made = Prompt.objects.get_or_create(
                name=spec.name,
                defaults={"body": spec.prompt_body or "", "description": spec.description},
            )
            prompts_created += int(made)
            Prompt.objects.filter(name=spec.name, description="").update(description=spec.description)
        defaults = {
            "delay_seconds": spec.delay_seconds,
            "daily_at": spec.daily_at,
            "description": spec.description,
            "enabled": spec.default_enabled,
            "colleague_facing": spec.colleague_facing,
        }
        if prompt is not None:
            defaults["prompt"] = prompt
        else:
            defaults["script"] = spec.script_entry_point
        _, made = Loop.objects.get_or_create(name=spec.name, defaults=defaults)
        loops_created += int(made)
        Loop.objects.filter(name=spec.name, description="").update(description=spec.description)
    return SeedResult(loops_created=loops_created, prompts_created=prompts_created)
