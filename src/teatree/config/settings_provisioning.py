"""Provisioning and stack-reaper settings declarations."""

from dataclasses import dataclass
from typing import ClassVar, Final

from teatree.config.declaration import SettingSpec, setting
from teatree.config.setting_parsers import _parse_str_list, _parse_strict_float, _parse_strict_int

WRITE_CONCURRENCY_PER_CORE: Final = 0.5
WRITE_CONCURRENCY_PER_CORE_MIN: Final = 0.25
WRITE_CONCURRENCY_PER_CORE_MAX: Final = 2.0
ADMISSION_LOAD_BRAKE_PER_CORE: Final = 2.0
ADMISSION_LOAD_RESUME_PER_CORE: Final = 1.5
ADMISSION_LOAD_FACTOR_MIN: Final = 0.5
ADMISSION_LOAD_FACTOR_MAX: Final = 6.0


@dataclass
class _ProvisioningSettings:
    """Provisioning timeouts / concurrency + the idle-stack, stale-stack, and queue reaper knobs."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Infrastructure", "Provisioning")

    # #2220 Hard ceiling (seconds) for one long-blocking provisioning subprocess
    # — a DSLR snapshot restore, ``migrate``, or a ``--create-db`` test-DB
    # rebuild. On exceeding it the step ABORTS with an actionable error AND
    # fires a loud out-of-band user alert, instead of grinding silently for an
    # hour (the recurring "frozen sub-agent" symptom, e.g. a forked migration
    # graph). The default is generous (30 min) so a healthy restore+migrate
    # never trips it; a forked graph or a true hang blows past it and gets
    # aborted+alerted. A non-positive value degrades to the default — the
    # "never hang" invariant cannot be configured away. Per-overlay overridable.
    provision_step_timeout_seconds: int = setting(
        1800, spec=SettingSpec(help="seconds an ordinary provisioning step may take", parse=_parse_strict_int)
    )
    # Parallel ticket-workspace provisioning speed + resource-aware admission.
    # A fast step (symlinks, settings, a compose override) defaults to this
    # short ceiling; only a step explicitly marked ``ProvisionStep.heavy``
    # (a DB import, a frontend build) keeps ``provision_step_timeout_seconds``.
    # Per-overlay overridable.
    provision_fast_step_timeout_seconds: int = setting(
        120, spec=SettingSpec(help="seconds a step declared fast may take before it is failed", parse=_parse_strict_int)
    )
    # nCPU-derived default concurrency cap for the bounded subprocess pool
    # ``workspace provision`` runs worktrees under. ``0`` (the default)
    # auto-derives from the host's ``os.cpu_count()`` at each read
    # (:func:`teatree.utils.ram_probe.default_provision_concurrency`); a
    # positive value pins an explicit cap. Per-overlay overridable.
    provision_max_concurrency: int = setting(
        0, spec=SettingSpec(help="provisioning steps that may run at once", parse=_parse_strict_int)
    )
    # #3644 The adaptive admission governor's default-ON flag. When true, every
    # dispatcher consults ``teatree.core.admission_governor`` before admitting work, so
    # token quota (primary) and machine load (secondary) bound concurrency and the
    # static concurrency settings become CEILINGS rather than targets. Setting it false
    # is the KILL-SWITCH and the rollback lever: admission reverts byte-for-byte to the
    # pre-governor static behaviour. Per-overlay overridable.
    # #4508 The admission-pressure scalar at which the EXPENSIVE agent class is refused
    # while the CHEAP review/ship lanes keep draining. 1.0 collapses SHED into HALT —
    # the rollback lever, restoring pre-#4508 admission byte-for-byte. Clamped into
    # [0.5, HALT_AT] on read so a typo cannot wedge the lane. Per-overlay overridable.
    admission_pressure_shed_at: float = setting(
        0.9,
        spec=SettingSpec(
            help=("pressure at which expensive agents are refused while review and ship keep draining"),
            parse=_parse_strict_float,
        ),
    )
    admission_load_brake_per_core: float = setting(
        ADMISSION_LOAD_BRAKE_PER_CORE,
        spec=SettingSpec(
            help="box load per core at which new admissions halt", parse=_parse_strict_float, box_global=True
        ),
    )
    admission_load_resume_per_core: float = setting(
        ADMISSION_LOAD_RESUME_PER_CORE,
        spec=SettingSpec(
            help="box load per core below which halted admissions resume", parse=_parse_strict_float, box_global=True
        ),
    )
    # Box-global: the reader sizes this box's cores, so a per-overlay row cannot be honoured.
    admission_write_concurrency_per_core: float = setting(
        WRITE_CONCURRENCY_PER_CORE,
        spec=SettingSpec(
            help=("agents the admission governor admits per CPU core before weekly pacing, in every dispatch lane"),
            parse=_parse_strict_float,
            box_global=True,
        ),
    )
    # #4816 Whether the TOKEN brakes (the subscription quota family and the metered
    # one) apply at all. False drops both and leaves load + memory, so an operator
    # standing down a quota signal their lane does not answer to keeps the brakes that
    # protect the box.
    # Per-overlay overridable.
    # #4816 The metered lane's own spend ceiling, in TOKENS over
    # ``metered_spend_window_hours``. Tokens because they are MEASURED; the lane's
    # cost_usd is price-table arithmetic whose error is unknown. Ships 0 = UNSET: the
    # operator's provider-side cycle limit is not readable from here and teatree must
    # not invent a budget, so the metered brake is inert until this is set.
    # Per-overlay overridable.
    metered_token_ceiling: int = setting(
        0,
        spec=SettingSpec(
            help=("the metered lane's spend ceiling in tokens over the window below; 0 leaves it unset"),
            parse=_parse_strict_int,
        ),
    )
    # #4816 The window ``metered_token_ceiling`` is measured over. Separate from the
    # amount because how much and over how long are two independent operator facts,
    # and the provider's cycle boundaries are not discoverable from teatree's data.
    # Per-overlay overridable.
    metered_spend_window_hours: int = setting(
        24, spec=SettingSpec(help="the window the metered token ceiling is measured over", parse=_parse_strict_int)
    )
    # Each cheap class is capped at this width (min 1) inside a lane one seat wider;
    # machine brakes spare both classes, while quota brakes still apply.
    cheap_phase_admission_ceiling: int = setting(
        2,
        spec=SettingSpec(
            help=(
                "per-class width (min 1): reviews and shipping/review requests are each "
                "capped at this width inside a cheap lane one seat wider; machine "
                "brakes spare both classes"
            ),
            parse=_parse_strict_int,
        ),
    )
    # #4163 RAM one pytest-xdist worker is sized at when the governor derives the
    # per-agent worker cap. Measured p90 worker RSS from the kernel OOM log,
    # 2026-07-21..08-04: p50 0.65 GB, p90 1.24 GB, max 23.1 GB. The p90 is the sizing
    # point — the max is one pathological run, not a budget, and sizing at the p50 is
    # what put 16 workers x 1.24 GB = 19.8 GB against 19.7 GB usable. A non-positive
    # value drops the memory term, leaving the cores-derived bound: the rollback lever,
    # and the same bounded fail-safe an unreadable probe takes. Per-overlay overridable.
    test_worker_ram_gb: float = setting(
        1.25,
        spec=SettingSpec(
            help="RAM one pytest worker is sized at when the governor caps test parallelism", parse=_parse_strict_float
        ),
    )
    # Repos that are ONE branch wide while listed — ``<repo-slug>=<branch>`` entries.
    # While a repo is listed, provisioning a worktree on any OTHER branch is
    # refused, as are the raw ``git worktree add`` / ``checkout -b`` / ``switch
    # -c`` / ``branch <name>`` / non-pinned ``push`` forms
    # (:mod:`teatree.core.gates.single_branch_repo_guard`). The case it exists for
    # is a fork bootstrap: the repo's whole reviewed history is in flight behind
    # one open PR, so there is nothing to base a second branch ON, and the
    # measured cost of leaving that to prose was 31 worktrees across 37 branches
    # on two repos, two of them redoing a fix already in flight. Cleanup verbs
    # (``worktree list/remove/prune``, ``branch -D``) stay allowed — they are how a
    # repo that already sprawled gets back into line. REMOVING an entry is how the
    # rule ends when that PR merges. Per-overlay overridable.
    # Its kill-switch (``single_branch_repo_gate_enabled``) is a COLD-HOOK setting
    # rather than a field here, for the same reason ``main_clone_guard_gate_enabled``
    # is: the Bash seam reads it from a cold hook with no importable teatree.
    single_branch_repos: list[str] = setting(
        default_factory=list,
        spec=SettingSpec(help="repos that may carry only one branch at a time", parse=_parse_str_list),
    )
    # RAM-used-percent ceiling above which a new provision is HELD (queued,
    # not started) rather than admitted, so a cold multi-repo provision never
    # pushes the host into OOM. 75 rather than the self-improve budget gate's
    # ``DEFAULT_RAM_USED_CEILING_PCT`` (85): provisioning arrives in bursts a
    # per-sample gate reads too late, and every live box already pins 75.
    # Per-overlay overridable.
    provision_ram_ceiling_percent: int = setting(
        75,
        spec=SettingSpec(help="percent of RAM provisioning may consume before it backs off", parse=_parse_strict_int),
    )
    # A provision whose total duration exceeds this many seconds fires a
    # best-effort out-of-band user alert — a regression in provisioning speed
    # must never be silently absorbed. Per-overlay overridable.
    provision_slow_threshold_seconds: int = setting(
        600,
        spec=SettingSpec(help="seconds after which a provisioning step is reported as slow", parse=_parse_strict_int),
    )
    # #2190 Idle-stack reaper — a loop scanner that stops the docker stack of
    # an IDLE locally-running worktree (``services_up``/``ready``) and demotes
    # it to ``provisioned`` (REVERSIBLE: DB + worktree preserved), freeing the
    # host's RAM and a ``max_concurrent_local_stacks`` slot. Idle = no active
    # session/task on the ticket AND ``last_used_at`` older than
    # ``idle_stack_idle_minutes`` AND not the currently-active worktree AND no
    # active-delivery lease / recent E2E run / explicit pin (#2227).
    # Fail-safe: uncertainty ⇒ KEEP. All knobs are per-overlay overridable.
    idle_stack_idle_minutes: int = setting(
        30,
        spec=SettingSpec(
            help="minutes of inactivity after which a local stack counts as idle", parse=_parse_strict_int
        ),
    )
    # #2227 Recency window for the E2E-run KEEP guard: a worktree whose
    # ``Worktree.last_e2e_run`` is within this many minutes is the live target of
    # in-flight evidence work and is never reaped, even when otherwise idle.
    idle_stack_e2e_recent_minutes: int = setting(
        60, spec=SettingSpec(help="minutes an E2E run keeps a stack safe from the idle reaper", parse=_parse_strict_int)
    )
    # #2207 Stale-stack reaper — tears down docker compose stacks that NO
    # Worktree row owns (hand-rolled test stacks, failed-teardown leftovers)
    # once their newest container lifecycle event (created/started/finished)
    # is older than this many minutes. Age-keyed so a parallel session's
    # fresh manual stack is never reaped; an unknown age fails safe (keep).
    # Runs automatically before ``worktree start`` / ``workspace start`` /
    # ``workspace provision`` and on demand via
    # ``t3 <overlay> workspace reap-stale``. ``0`` disables the sweep; a test
    # that must not reach the developer's real docker daemon sets it to ``0``
    # rather than relying on the default, which has been ON since #2207 landed.
    # Per-overlay overridable.
    stale_stack_min_age_minutes: int = setting(
        240, spec=SettingSpec(help="minutes a stack must exist before staleness is judged", parse=_parse_strict_int)
    )
    # #2190/#44 Acquisition queue — when ``worktree start`` / ``workspace
    # start`` hits the cap, it reaps idle, retries, then ENQUEUES (no
    # SystemExit). A loop scanner drains the queue each tick with a
    # Fibonacci-minute backoff, never tearing down another ticket's stack.
    # `MAX_QUEUE_ATTEMPTS` caps the Fibonacci retries before a queued request
    # is marked DEAD and surfaced.
    # fnmatch globs of branch names ``clean-all`` must NEVER reap even when the
    # squash-merge classifier says shipped — never-merge dev overrides, long-lived
    # spikes. Matched against the full branch name. Default empty: nothing
    # protected beyond the data-loss guards. Per-overlay overridable.
    clean_ignore: list[str] = setting(
        default_factory=list,
        spec=SettingSpec(help="branch globs the cleanup reapers never touch", parse=_parse_str_list),
    )
