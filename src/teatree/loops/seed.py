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
    """One default loop's seed config — name, cadence, description, and how it is invoked.

    ``prompt_body`` set ⇒ a prompt-backed loop (a :class:`Prompt` named for the
    loop is seeded and the FK points at it); otherwise the loop is script-backed
    at its OWN module (:func:`script_entry_point_for`). ``daily_at`` overrides the
    interval for a once-per-day loop. ``description`` is the loop's real one-line
    "what it does and when" — the source of truth populated onto ``Loop.description``
    (and the prompt-backed loop's ``Prompt.description``) and rendered by
    ``t3 loops list``. ``colleague_facing`` (#2904) marks a loop that reaches or
    reads from a colleague — the #2904 admission gate skips it whenever
    availability defers questions (away / autonomous_away). ``default_enabled``
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


# One autonomous loop per row, each on its own cadence. Every script-backed loop
# points at its OWN module; ``arch_review`` is the single prompt-backed default.
DEFAULT_LOOPS: tuple[LoopSeedSpec, ...] = (
    LoopSeedSpec(
        "inbox",
        60,
        "Drains inbound Slack mentions, DMs, review-intent and RED-CARD reactions "
        "(plus the Notion view) into the DB every 1m and routes them.",
        default_enabled=True,
    ),
    LoopSeedSpec(
        "idle_stack_reaper",
        60,
        "Stops local dev stacks left idle past their threshold to free a concurrency slot; checks every 1m.",
        default_enabled=True,
    ),
    LoopSeedSpec(
        "local_stack_queue",
        60,
        "Drains the local-stack acquisition queue, starting the next queued worktree "
        "stack whose backoff retry is due; checks every 1m.",
        default_enabled=True,
    ),
    LoopSeedSpec(
        "resource_pressure",
        60,
        "Auto-frees host disk and RAM when they cross the pressure threshold; "
        "checks every 1m on its own ~5m internal cadence.",
        default_enabled=True,
    ),
    LoopSeedSpec(
        "snapshot_warmer",
        86400,
        "Refreshes each overlay-declared reference DB's DSLR snapshot out-of-band once a day "
        "so a ticket-critical-path provision never pays the slow restore+migrate path.",
    ),
    # NOTE: the reactive infra loops (``slack_answer`` / ``self_improve`` /
    # ``drain_queue``) are intentionally absent — they have no registry MiniLoop
    # and each runs as its own dedicated `/loop` (`t3 loop <slot> run`), never via
    # a per-loop tick (see the module docstring). A seeded row would be an orphan.
    LoopSeedSpec(
        "dispatch",
        300,
        "Runs the always-on global scanners every 5m: dispatches pending headless Tasks "
        "to phase sub-agents, ingests incoming events, redelivers undelivered notifies, "
        "and posts deferred questions.",
        default_enabled=True,
    ),
    LoopSeedSpec(
        "tickets",
        300,
        "Scans the local Ticket DB and each code host every 5m — surfacing active and "
        "stale tickets, dispositioning issues, and marking completed ones.",
        default_enabled=True,
    ),
    LoopSeedSpec(
        "review",
        300,
        "Reviews colleague-authored open PRs every 5m and posts inline findings (with the "
        "PR-sweep, codex double-check and Slack-broadcast helpers).",
        colleague_facing=True,
    ),
    LoopSeedSpec(
        "ship",
        300,
        "Sweeps your own-authored open PRs every 5m: folds in approvals/CI and executes "
        "the keystone merge of your PRs (consumes the orchestrator's MergeClear).",
    ),
    LoopSeedSpec(
        "pane_reaper",
        300,
        "Demotes idle Agent-Teams maker panes past the idle threshold every 5m; inert unless team mode is enabled.",
        default_enabled=True,
    ),
    LoopSeedSpec(
        "issue_disposition",
        300,
        "Auto-closes high-confidence DEAD backlog issues (already-shipped / duplicate / obsolete) "
        "every 5m; default-off behind auto_disposition_enabled, bounded per tick.",
    ),
    LoopSeedSpec(
        "audit",
        1800,
        "Verifies and posts per-overlay failed-E2E results to Slack (driven by overlay watchers) every 30m.",
    ),
    LoopSeedSpec(
        "followup",
        1800,
        "Intakes newly-assigned issues (auto-starting ready ones) and fires the review-request nag every 30m.",
        colleague_facing=True,
    ),
    LoopSeedSpec(
        "issue_implementer",
        3600,
        "Discovers and claims labelled backlog issues to auto-implement, kicking off the "
        "maker pipeline; hourly, default-off behind a triple gate.",
    ),
    LoopSeedSpec(
        "housekeeping",
        3600,
        "Fast-forwards the editable teatree and overlay installs (self-update) and pulls "
        "each overlay's main clone hourly.",
        default_enabled=True,
    ),
    LoopSeedSpec(
        "arch_review",
        10800,
        "Dispatches a sub-agent every 3h to run a holistic, codebase-wide architectural "
        "review via the ac-reviewing-codebase skill.",
        prompt_body=ARCH_REVIEW_PROMPT_BODY,
    ),
    LoopSeedSpec(
        "dogfood",
        86400,
        "Runs the overlay provisioning smoke test once a day to catch broken worktree setup.",
    ),
    LoopSeedSpec(
        "eval_local",
        86400,
        "Runs the local behavioral eval suite; the scanner enforces its own weekly cadence (checked daily).",
    ),
    LoopSeedSpec(
        "backlog_sweep",
        86400,
        "Sweeps the backlog daily to propose closing stale issues; default-off (destructive-capable) "
        "behind backlog_sweep_disabled, gated by ask_before_backlog_sweep_closes.",
    ),
    LoopSeedSpec(
        "news",
        86400,
        "Fires the daily news-scan task at 08:00 to surface relevant external releases and improvement ideas.",
        daily_at=dt.time(8, 0),
    ),
    LoopSeedSpec(
        "dream",
        86400,
        "Runs the nightly memory-consolidation pass at 03:00 — cross-link, merge, "
        "reindex MEMORY.md, decay — off the live tick.",
        daily_at=dt.time(3, 0),
    ),
    LoopSeedSpec(
        "outer_loop",
        86400,
        "Advances at most one T4 autoresearch experiment one step per day (propose, "
        "ratify, implement, measure, keep-only-if-better), off the live tick; ships "
        "disabled behind the outer_loop_enabled flag and the critic-live guard.",
    ),
    LoopSeedSpec(
        "directive_loop",
        86400,
        "Advances one ratified directive one step per day (implement, configure, verify, "
        "keep-only-if-verified, else human-asked revert), off the live tick; ships disabled "
        "behind the directive_loop_enabled flag and the critic-live guard.",
    ),
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
    """
    from teatree.core.models import Loop, Prompt  # noqa: PLC0415

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
