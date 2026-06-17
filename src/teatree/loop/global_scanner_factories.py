"""Global (non-overlay) cadence-scanner builders + the default-jobs assembler.

The teatree-CORE global scanners (news / provision-smoke / eval / self-update /
resource-pressure) plus ``build_default_jobs`` / ``build_default_scanners`` that
fan the global dispatch set + per-overlay slices into the tick. Depends DOWN on
``domain_jobs`` (``jobs_for_domain`` / ``_jobs_for_overlay_backend``). Carved out
of the loop tick fan-out to stay under the module-health LOC cap.
"""

import os
from pathlib import Path

from teatree.config import TeamsDisplay, discover_active_overlay, discover_overlays, get_effective_settings, load_config
from teatree.core.backend_factory import OverlayBackends
from teatree.core.backend_protocols import CodeHostBackend, MessagingBackend
from teatree.loop.domain_jobs import _jobs_for_overlay_backend, jobs_for_domain
from teatree.loop.job_identity import _CANONICAL_CORE_OVERLAY, Domain, _ScannerJob
from teatree.loop.scanners import (
    AssignedIssuesScanner,
    BacklogSweepScanner,
    EvalLocalScanner,
    IdleStackReaperScanner,
    LocalStackQueueDrainerScanner,
    MyPrsScanner,
    NotionViewScanner,
    PaneReaperScanner,
    RedCardScanner,
    ResourcePressureScanner,
    ReviewerPrsScanner,
    Scanner,
    ScanningNewsScanner,
    SelfUpdateScanner,
    SlackDmInboundScanner,
    SlackMentionsScanner,
    SlackReviewIntentScanner,
)
from teatree.loop.scanners.notion_view import NotionLike
from teatree.loop.scanners.self_update_ci import GhMainCiStatus


def _dogfood_smoke_scanner() -> Scanner | None:
    """Wire the global provision-smoke scanner (#1308)."""
    from teatree.loop.scanners.provision_smoke import build_provision_smoke_scanner  # noqa: PLC0415

    return build_provision_smoke_scanner(
        load_config=load_config,
        discover_active_overlay=discover_active_overlay,
        canonical_fallback=_CANONICAL_CORE_OVERLAY,
    )


def _collect_self_update_repos() -> list[tuple[str, Path]]:
    """Enumerate editable clones the self-update scanner should fast-forward (#1249).

    Returns ``(label, repo_path)`` pairs for the editable-installed
    teatree core clone plus every overlay clone discovered via
    :func:`teatree.config.discover_overlays`. The label is the human-
    friendly tag the scanner persists in :class:`SelfUpdateMarker`;
    ``"teatree"`` for core, the overlay's registered name for overlays.

    Targets stay in lockstep with what ``t3 update`` would touch: the
    teatree core clone first, then each overlay's ``project_path``
    resolved to its git toplevel. A repo wins exactly once even when
    two paths resolve to the same toplevel.
    """
    repos: list[tuple[str, Path]] = []
    seen: set[Path] = set()

    core = _resolve_t3_repo()
    if core is not None:
        repos.append(("teatree", core))
        seen.add(core)

    for entry in discover_overlays():
        if entry.project_path is None:
            continue
        toplevel = _git_toplevel(entry.project_path.expanduser())
        if toplevel is None or toplevel in seen:
            continue
        seen.add(toplevel)
        repos.append((entry.name, toplevel))
    return repos


def _resolve_t3_repo() -> Path | None:
    """Resolve the editable teatree clone path from the ``T3_REPO`` env var.

    Returns ``None`` when the env var is unset, points at a missing
    directory, or points at a directory that does not look like a
    teatree clone (no ``pyproject.toml`` + ``.git``). Worktrees still
    qualify — ``.git`` may be a file pointing at the main clone's
    object store, which is the same shape ``t3 update`` handles.
    """
    env_path = os.environ.get("T3_REPO", "")
    if not env_path:
        return None
    candidate = Path(env_path).expanduser()
    if not (candidate / "pyproject.toml").is_file():
        return None
    git_entry = candidate / ".git"
    if not (git_entry.is_dir() or git_entry.is_file()):
        return None
    return candidate.resolve()


def _git_toplevel(path: Path) -> Path | None:
    """Return the git work-tree root containing *path*, or ``None`` if not a repo."""
    from teatree.utils.run import run_allowed_to_fail  # noqa: PLC0415

    if not path.is_dir():
        return None
    result = run_allowed_to_fail(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=path,
        expected_codes=None,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip()).resolve()


def _self_update_scanner() -> SelfUpdateScanner | None:
    """Build the global self-update scanner from teatree-core config (#1249, #1760).

    Returns ``None`` when ``self_update_disabled = true`` (the escape
    hatch) OR when there are no editable clones to walk (a non-editable
    install with no registered overlay project paths — nothing to pull).
    Otherwise builds a single global :class:`SelfUpdateScanner` whose
    cadence honours the ``self_update_cadence_hours`` setting (default
    1 hour). The scanner is wired as a global job (``overlay=""``)
    because it concerns the editable installs themselves, not any one
    overlay's tracked work.

    #1760 wires the CI-green fail-closed gate and the deferred-reinstall
    queue: ``auto_update_require_green_main`` (default ON) refuses a
    ff-pull unless the default branch's CI is explicitly green — the
    verdict comes from :class:`GhMainCiStatus`, the same ``gh
    check-runs`` source the PR sweep uses. ``auto_update_reinstall``
    (default OFF, ``T3_LOOP_AUTO_UPDATE`` env wins) opts into queuing a
    deferred reinstall on an actual update.
    """
    settings = load_config().user
    if settings.self_update_disabled:
        return None
    repos = _collect_self_update_repos()
    if not repos:
        return None
    return SelfUpdateScanner(
        repos=tuple(repos),
        cadence_hours=settings.self_update_cadence_hours,
        ci_status=GhMainCiStatus(),
        require_green_main=settings.auto_update_require_green_main,
        auto_update_reinstall=settings.auto_update_reinstall,
    )


def _resource_pressure_scanner() -> ResourcePressureScanner | None:
    """Build the global resource-pressure scanner from teatree-core config (#128).

    Returns ``None`` when ``resource_pressure_disabled = true`` (the durable
    kill-switch, mirroring ``self_update_disabled``) so the job is never wired.
    Otherwise builds a single global :class:`ResourcePressureScanner`
    (``overlay=""``) — disk/RAM pressure is a host-level concern, not any one
    overlay's tracked work. All thresholds, cadence, allow-lists, and
    destructive opt-in flags come straight from ``UserSettings``; the
    destructive levers default OFF.
    """
    settings = load_config().user
    if settings.resource_pressure_disabled:
        return None
    return ResourcePressureScanner(
        disk_warn_free_gb=settings.disk_warn_free_gb,
        disk_crit_free_gb=settings.disk_crit_free_gb,
        ram_warn_avail_gb=settings.ram_warn_avail_gb,
        ram_crit_avail_gb=settings.ram_crit_avail_gb,
        cadence_minutes=settings.resource_pressure_cadence_minutes,
        min_free_interval_minutes=settings.resource_pressure_min_free_interval_minutes,
        disk_cache_allowlist=tuple(settings.disk_cache_allowlist),
        allow_destructive_disk=settings.allow_destructive_disk,
        worktree_stale_days=settings.worktree_stale_days,
        max_worktree_gc_per_tick=settings.max_worktree_gc_per_tick,
        allow_destructive_ram=settings.allow_destructive_ram,
        ram_kill_allowlist=tuple(settings.ram_kill_allowlist),
    )


def _idle_stack_reaper_scanner() -> IdleStackReaperScanner | None:
    """Build the global idle-stack reaper scanner from teatree-core config (#2190).

    Returns ``None`` when ``idle_stack_reaper_disabled = true`` (the durable
    kill-switch, mirroring ``resource_pressure_disabled``). The reaper is
    per-overlay scoped — it stops idle stacks of the active overlay — so the
    overlay anchor is resolved via :func:`discover_active_overlay`, falling
    back to the canonical core overlay when none is registered (mirrors
    :func:`_scanning_news_scanner`).
    """
    settings = load_config().user
    if settings.idle_stack_reaper_disabled:
        return None
    active = discover_active_overlay()
    overlay_name = active.name if active is not None else _CANONICAL_CORE_OVERLAY
    return IdleStackReaperScanner(
        overlay=overlay_name,
        idle_minutes=settings.idle_stack_idle_minutes,
        cadence_minutes=settings.idle_stack_reaper_cadence_minutes,
    )


def _local_stack_queue_drainer_scanner() -> LocalStackQueueDrainerScanner | None:
    """Build the global acquisition-queue drainer scanner from config (#2190, #44).

    Returns ``None`` when ``local_stack_queue_disabled = true`` (the durable
    kill-switch). Per-overlay scoped like the reaper; the per-item Fibonacci
    backoff IS the cadence (carried on the row), so no marker is wired.
    """
    settings = load_config().user
    if settings.local_stack_queue_disabled:
        return None
    active = discover_active_overlay()
    overlay_name = active.name if active is not None else _CANONICAL_CORE_OVERLAY
    return LocalStackQueueDrainerScanner(overlay=overlay_name)


def _pane_reaper_scanner() -> PaneReaperScanner | None:
    """Build the global idle-maker-pane reaper scanner from teatree config (#1838 PR#7b).

    Returns ``None`` when ``teams_enabled`` is false — the DEFAULT-OFF path, so
    installing the pane-reaper mini-loop changes no behaviour until the user
    flips the feature on. The reaper is a global concern (it demotes any idle
    ``team:<role>`` claim regardless of overlay), so it carries no overlay
    anchor; the idle threshold is ``teams_idle_minutes``. The in-scanner
    ``teams_enabled`` guard is belt-and-braces with this ``None``-when-off
    return, so neither the mini-loop nor the scanner can act while teams is off.
    """
    settings = get_effective_settings()
    if not settings.teams_enabled:
        return None
    return PaneReaperScanner(
        teams_enabled=True,
        idle_minutes=settings.teams_idle_minutes,
        display_enabled=settings.teams_display is not TeamsDisplay.NONE,
    )


def _scanning_news_scanner() -> ScanningNewsScanner | None:
    """Build a global scanning-news scanner from teatree-core config.

    #1191: the news-scan cadence is a teatree-core platform behaviour
    that runs once per day regardless of which overlays are registered.
    The settings live on :class:`teatree.config.UserSettings` (the
    ``[teatree]`` table in ``~/.teatree.toml``, with optional per-overlay
    overrides). Returns ``None`` when ``scanning_news_disabled = true``
    (the escape hatch).

    #1267: the overlay-anchor identity is resolved via
    :func:`teatree.config.discover_active_overlay` rather than baked
    into the scanner module. Falls back to the canonical post-0027
    overlay name (``t3-teatree``) when no overlay is registered.

    #1391: ``ask_before_creating_news_tickets`` (default true) is the
    ask-gate flag threaded into the scanner so the queued task instructs
    the skill to record candidates for approval instead of auto-filing
    issues.
    """
    settings = load_config().user
    if settings.scanning_news_disabled:
        return None
    active = discover_active_overlay()
    overlay_name = active.name if active is not None else _CANONICAL_CORE_OVERLAY
    return ScanningNewsScanner(
        overlay_name=overlay_name,
        skill=settings.scanning_news_skill,
        cadence_hours=settings.scanning_news_cadence_hours,
        require_approval=settings.ask_before_creating_news_tickets,
    )


def _eval_local_scanner() -> EvalLocalScanner | None:
    """Build a global local-eval scanner from teatree-core config.

    User directive (2026-06-05): "AI evals should be run locally from
    time to time, and in CI once a week." The CI half lives in
    ``.github/workflows/ci.yml`` (``eval-weekly``); this is the local
    half. The cadence is a teatree-core platform behaviour (weekly by
    default), so the settings live on :class:`teatree.config.UserSettings`
    (the ``[teatree]`` table, per-overlay overridable). Returns ``None``
    when ``eval_local_disabled = true`` (the escape hatch).

    The overlay-anchor identity is resolved via
    :func:`teatree.config.discover_active_overlay`, falling back to the
    canonical post-0027 overlay name (``t3-teatree``) when no overlay is
    registered — mirroring :func:`_scanning_news_scanner`.
    """
    settings = load_config().user
    if settings.eval_local_disabled:
        return None
    active = discover_active_overlay()
    overlay_name = active.name if active is not None else _CANONICAL_CORE_OVERLAY
    return EvalLocalScanner(
        overlay_name=overlay_name,
        skill=settings.eval_local_skill,
        cadence_hours=settings.eval_local_cadence_hours,
    )


def _backlog_sweep_scanner() -> BacklogSweepScanner | None:
    """Build a global backlog-sweep scanner from teatree-core config (#2419).

    DEFAULT-OFF: ``backlog_sweep_disabled`` defaults *true*, so this
    builder returns ``None`` until the user opts in. The sweep is
    destructive-capable (it can propose closing issues), so unlike the
    always-on news/eval scanners the kill switch ships ON.

    The overlay-anchor identity is resolved via
    :func:`teatree.config.discover_active_overlay`, falling back to the
    canonical overlay name (``t3-teatree``) when no overlay is registered
    — mirroring :func:`_scanning_news_scanner`.

    ``ask_before_backlog_sweep_closes`` (default true) is the ask-gate
    flag threaded into the scanner so the queued task instructs the
    skill to record close proposals for approval instead of mass-closing.
    """
    settings = load_config().user
    if settings.backlog_sweep_disabled:
        return None
    active = discover_active_overlay()
    overlay_name = active.name if active is not None else _CANONICAL_CORE_OVERLAY
    return BacklogSweepScanner(
        overlay_name=overlay_name,
        skill=settings.backlog_sweep_skill,
        cadence_hours=settings.backlog_sweep_cadence_hours,
        require_approval=settings.ask_before_backlog_sweep_closes,
    )


def build_default_jobs(
    *,
    backends: list[OverlayBackends] | None = None,
    host: CodeHostBackend | None = None,
    messaging: MessagingBackend | None = None,
    notion_client: NotionLike | None = None,
    ready_labels: tuple[str, ...] = (),
) -> list[_ScannerJob]:
    """Build the default scanner jobs from one or more overlays.

    Pass *backends* to scan multiple overlays in one tick (each gets its
    own host/messaging credentials). The *host*/*messaging* shape
    is preserved for callers that resolve a single overlay themselves.
    """
    jobs: list[_ScannerJob] = jobs_for_domain(Domain.DISPATCH)
    # #1191 Periodic scanning-news scanner — teatree-CORE global (not
    # per-overlay). Daily cadence is teatree-platform config; the queued
    # task is anchored on the `teatree` overlay placeholder ticket so
    # the dispatcher routes through the standard pending-task pipeline.
    # #1191 / #1308 — global teatree-CORE scanners (news + provision smoke).
    # #2419 backlog-sweep is a global teatree-CORE scanner too, but ships
    # DEFAULT-OFF (its kill switch defaults ON): the builder returns None
    # until the user opts in, so the ``if s`` filter naturally excludes it.
    jobs.extend(
        _ScannerJob(scanner=s, overlay="")
        for s in (
            _scanning_news_scanner(),
            _dogfood_smoke_scanner(),
            _eval_local_scanner(),
            _backlog_sweep_scanner(),
        )
        if s
    )
    # #1249 Self-update scanner — fast-forwards the editable teatree
    # core clone + every registered overlay clone to ``origin/<default>``
    # once the cadence has elapsed. Wired as a global job because it
    # concerns the editable installs themselves, not any one overlay's
    # tracked work.
    self_update_scanner = _self_update_scanner()
    if self_update_scanner is not None:
        jobs.append(_ScannerJob(scanner=self_update_scanner, overlay=""))
    # #128 Resource-pressure scanner — global (overlay="") host-level
    # disk/RAM auto-free. Monitoring + regenerable-cache purge on by
    # default; destructive levers flag-gated off. Kill-switch:
    # ``resource_pressure_disabled = true`` → builder returns None.
    resource_pressure_scanner = _resource_pressure_scanner()
    if resource_pressure_scanner is not None:
        jobs.append(_ScannerJob(scanner=resource_pressure_scanner, overlay=""))
    # #2190 idle-stack reaper + #44 acquisition-queue drainer — global
    # (overlay="") mechanical scanners. The reaper stops idle stacks to free a
    # ``max_concurrent_local_stacks`` slot; the drainer re-fires a queued
    # ``start`` once a slot frees. Kill-switches: ``idle_stack_reaper_disabled``
    # / ``local_stack_queue_disabled`` → builder returns None.
    idle_stack_reaper_scanner = _idle_stack_reaper_scanner()
    if idle_stack_reaper_scanner is not None:
        jobs.append(_ScannerJob(scanner=idle_stack_reaper_scanner, overlay=""))
    queue_drainer_scanner = _local_stack_queue_drainer_scanner()
    if queue_drainer_scanner is not None:
        jobs.append(_ScannerJob(scanner=queue_drainer_scanner, overlay=""))

    if backends:
        all_backends = tuple(backends)
        for backend in backends:
            jobs.extend(_jobs_for_overlay_backend(backend, all_backends=all_backends))
    else:
        if host is not None:
            jobs.extend(
                [
                    _ScannerJob(scanner=MyPrsScanner(host=host), overlay=""),
                    _ScannerJob(scanner=ReviewerPrsScanner(host=host), overlay=""),
                    _ScannerJob(scanner=AssignedIssuesScanner(host=host, ready_labels=ready_labels), overlay=""),
                ],
            )
        if messaging is not None:
            jobs.extend(
                [
                    _ScannerJob(scanner=SlackMentionsScanner(backend=messaging), overlay=""),
                    _ScannerJob(scanner=SlackDmInboundScanner(backend=messaging, overlay=""), overlay=""),
                    _ScannerJob(scanner=SlackReviewIntentScanner(backend=messaging, overlay=""), overlay=""),
                    # #1130 RED CARD detection for the single-overlay path.
                    _ScannerJob(scanner=RedCardScanner(backend=messaging, overlay=""), overlay=""),
                ]
            )

    if notion_client is not None:
        jobs.append(_ScannerJob(scanner=NotionViewScanner(client=notion_client), overlay=""))
    return jobs


def build_default_scanners(
    *,
    host: CodeHostBackend | None,
    messaging: MessagingBackend | None,
    notion_client: NotionLike | None = None,
    ready_labels: tuple[str, ...] = (),
) -> list[Scanner]:
    """Single-overlay scanner builder kept for tests and ad-hoc CLI use."""
    return [
        job.scanner
        for job in build_default_jobs(
            host=host,
            messaging=messaging,
            notion_client=notion_client,
            ready_labels=ready_labels,
        )
    ]
