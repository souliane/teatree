"""``_check_*`` probes for clone/install/venv hygiene invoked by `t3 doctor check`.

Each helper is narrow (single concern, single ``typer.echo`` path) and returns
``bool`` for pass/fail aggregation by :func:`teatree.cli.doctor.app.run_doctor_checks`.
"""

import os
from pathlib import Path

import typer

_DEFAULT_TMPFS_WARN_PERCENT = 80
_PERCENT_MAX = 100
_MIN_MOUNT_FIELDS = 3


def _tmpfs_warn_percent(raw: str | None) -> int:
    """Parse ``TEATREE_TMPFS_WARN_PERCENT`` into a 1..100 threshold; default on garbage."""
    if raw is None:
        return _DEFAULT_TMPFS_WARN_PERCENT
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_TMPFS_WARN_PERCENT
    return value if 1 <= value <= _PERCENT_MAX else _DEFAULT_TMPFS_WARN_PERCENT


def _tmp_mount_fstype(mounts_text: str, mount_point: str) -> str | None:
    """Return the fstype backing *mount_point* per ``/proc/mounts`` text, or ``None``.

    Reads the standard ``/proc/mounts`` columns (device, mount point, fstype, ...)
    and returns the LAST matching entry's fstype — a later mount over the same point
    shadows an earlier one. ``None`` when *mount_point* is not mounted.
    """
    fstype: str | None = None
    for line in mounts_text.splitlines():
        fields = line.split()
        if len(fields) >= _MIN_MOUNT_FIELDS and fields[1] == mount_point:
            fstype = fields[2]
    return fstype


def _check_tmp_tmpfs_headroom(
    *,
    mounts_path: Path = Path("/proc/mounts"),
    tmp_dir: str = "/tmp",  # noqa: S108 — auditing the /tmp mount, not creating a temp file
) -> bool:
    """WARN when a RAM-backed (tmpfs) ``/tmp`` is filling toward ENOSPC.

    The box's ``/tmp`` is a small RAM tmpfs; agent ``claude`` sessions, pytest, and
    uv scratch can fill it to 100% and wedge everything with ENOSPC. Runtime temp is
    now routed to DISK (``deploy/entrypoint.sh`` + the managed settings-template
    ``TMPDIR``), but this surfaces residual tmpfs pressure directly so a fill is SEEN
    before it wedges the box. Only meaningful when ``/tmp`` is actually tmpfs — a
    disk-backed ``/tmp`` (e.g. the container overlay) is silently skipped, as is a
    box with no ``/proc/mounts`` (non-Linux). Surfacing-only: a WARN that keeps the
    run GREEN (never extracted into the watchdog FAIL DM), matching the sibling
    advisory checks. Threshold overridable via ``TEATREE_TMPFS_WARN_PERCENT`` (1..100,
    default 80). Crash-proof — any probe error degrades to a silent pass so this
    diagnostic never aborts the doctor run.
    """
    try:
        if not mounts_path.is_file():
            return True
        if _tmp_mount_fstype(mounts_path.read_text(encoding="utf-8"), tmp_dir) != "tmpfs":
            return True
        threshold = _tmpfs_warn_percent(os.environ.get("TEATREE_TMPFS_WARN_PERCENT"))
        stats = os.statvfs(tmp_dir)
        total = stats.f_blocks * stats.f_frsize
        if total <= 0:
            return True
        used_pct = round((total - stats.f_bavail * stats.f_frsize) / total * 100)
        if used_pct >= threshold:
            typer.echo(
                f"WARN  {tmp_dir} is a RAM-backed tmpfs at {used_pct}% used (>= {threshold}% threshold) — "
                f"agent/pytest/uv scratch can fill it to ENOSPC and wedge the box. Trim it: "
                f"`find {tmp_dir} -maxdepth 1 -name 'pytest-*' -mmin +120 -exec rm -rf {{}} +`. Runtime "
                "temp is routed to disk via TMPDIR; tune this with TEATREE_TMPFS_WARN_PERCENT."
            )
    except OSError:
        return True
    return True


def _check_single_db() -> bool:
    """Warn if any ``db.sqlite3`` other than the canonical path exists under DATA_DIR."""
    from teatree.paths import CANONICAL_DB, DATA_DIR, find_stale_dbs  # noqa: PLC0415 — deferred: lazy CLI import

    stale = list(find_stale_dbs(DATA_DIR, canonical=CANONICAL_DB))
    if not stale:
        return True
    for path in stale:
        typer.echo(f"WARN  Stale DB at {path} — canonical DB is {CANONICAL_DB}. Remove to silence.")
    return False


def _check_entrypoint_is_primary_clone() -> bool:
    """FAIL when the running ``t3`` entrypoint is anchored to a worktree (#1507).

    The installed long-lived ``t3`` must import ``teatree`` from the primary
    clone. A stale editable ``.pth`` anchored to a worktree makes the resident
    code resolve a per-worktree isolated DB (``paths.DATA_DIR_AUTO_ISOLATED``
    is then ``True``) while the loop and canonical state live in the true
    canonical DB — work silently vanishes. This is a hard FAIL, not a WARN,
    naming the offending worktree, both DB paths, and the remediation.

    Reads the live :mod:`teatree.paths` attributes (resolved at that module's
    import time from the entrypoint's on-disk location), so it reports the
    state of the process actually running ``t3 doctor``.
    """
    import teatree  # noqa: PLC0415 — deferred: keeps CLI startup light
    from teatree import paths  # noqa: PLC0415 — deferred: keeps CLI startup light

    if not paths.DATA_DIR_AUTO_ISOLATED:
        return True
    # ``teatree.__file__`` is ``<repo>/src/teatree/__init__.py``; the repo root
    # is its third parent (matches ``paths._code_repo_root``).
    repo_root = Path(teatree.__file__).resolve().parents[2]
    isolated_db = paths.DATA_DIR / "db.sqlite3"
    typer.echo(
        f"FAIL  Entrypoint is anchored to a worktree, not the primary clone: {repo_root}. "
        f"The installed t3 resolves the isolated DB {isolated_db} instead of the canonical "
        f"DB {paths.TRUE_CANONICAL_DB} — loop state and merges silently diverge. Re-anchor "
        f"the editable install at the primary clone: re-run `t3 setup` from the primary "
        f"clone (or fix the stale `.pth`), then re-run `t3 doctor check`.",
    )
    return False


def _check_dangling_editable_pth() -> bool:
    """FAIL when the teatree editable ``.pth`` or uv receipt points at a gone dir.

    The reaped-worktree footgun: a sub-agent repointed the GLOBAL uv-tool
    ``teatree.pth`` at its own worktree, which ``clean-all`` later reaped, leaving
    the ``.pth`` dangling so every ``t3`` died with ``ModuleNotFoundError: No
    module named 't3_bootstrap'`` machine-wide. This detects that dangling state
    (and the sibling uv-receipt ``editable`` clone) before it can wedge the next
    invocation, and auto-repairs the ``.pth`` to ``$T3_REPO/src`` when it is SAFE
    to do so — i.e. only when the running ``t3`` is already importing teatree from
    the canonical clone (never from a worktree, which would re-anchor the global
    install at a transient checkout, the #1507 hazard).

    A healthy install passes silently. Crash-proof: any unexpected error degrades
    to a pass so this diagnostic never aborts the whole doctor run.
    """
    from teatree.utils.editable_pth import (  # noqa: PLC0415 — deferred: keeps CLI startup light
        canonical_src_dir,
        detect_dangling_editable,
        repair_pth_to_canonical,
        running_from_canonical_clone,
    )

    try:
        dangling = detect_dangling_editable()
    except Exception as exc:  # noqa: BLE001 — an inspection failure warns and passes, never blocks doctor
        typer.echo(f"WARN  Could not inspect the teatree editable .pth: {exc}")
        return True
    if not dangling.is_dangling:
        return True

    canonical = canonical_src_dir()
    pth = dangling.pth
    if (
        pth is not None
        and dangling.pth_dangling_dir is not None
        and canonical is not None
        and running_from_canonical_clone()
        and repair_pth_to_canonical(pth, canonical)
    ):
        typer.echo(
            f"WARN  Repaired dangling teatree editable .pth (was {dangling.pth_dangling_dir}, "
            f"now {canonical}). The reaped worktree it pointed at would have broken t3 machine-wide."
        )
        # Re-evaluate after the repair so a stale, pre-repair snapshot does not
        # FAIL on (and tell the user to re-anchor) a .pth this run just healed.
        # Any genuinely-unrelated receipt problem is preserved by the re-detect.
        dangling = detect_dangling_editable()
        if not dangling.is_dangling:
            return True

    pth_still_dangling = dangling.pth_dangling_dir is not None
    if pth_still_dangling:
        typer.echo(
            f"FAIL  teatree editable .pth points at a non-existent dir: {dangling.pth_dangling_dir} "
            f"({dangling.pth}). A reaped worktree left it dangling — t3 dies with "
            f"ModuleNotFoundError. Re-anchor: re-run `t3 setup` from the canonical clone "
            f"(or rewrite the .pth to $T3_REPO/src), then `cd $T3_REPO && uv tool install --editable . --force`."
        )
    if dangling.receipt_source is not None:
        typer.echo(
            f"FAIL  uv tool receipt records a non-existent editable source: {dangling.receipt_source}. "
            f"It re-breaks the .pth on the next `t3 update`/reinstall. Fix: "
            f"`cd $T3_REPO && uv tool install --editable . --force`."
        )
    return False


def _check_t3_shim_receipt(*, repair: bool = False) -> bool:
    """FAIL when the active ``t3`` shim serves an editable install from the wrong checkout (#3231).

    A second, unrelated ``uv tool install --editable <other-checkout>`` under the
    same ``teatree`` package/entrypoint name silently steals the global ``t3``
    shim — and a moved/renamed checkout re-points the receipt at a stale path.
    Either way the receipt's ``requirements[].editable`` no longer matches the
    expected checkout (``$T3_REPO``), yet ``t3`` keeps resolving against the
    wrong source until a command fails deep inside. Unlike the dangling-``.pth``
    check (which fires only when the target is GONE), this catches a target that
    EXISTS but is the wrong clone.

    Only meaningful with a known expected checkout: when ``$T3_REPO`` is unset,
    or the install is not an editable uv-tool install (no receipt editable
    source), the check skips (returns ``True``). On a mismatch it FAILs with the
    remediation; with ``repair=True`` it re-points the install via
    ``uv tool install --editable <checkout> --force`` and passes. Crash-proof:
    any inspection failure degrades to a pass so it never aborts the doctor run.
    """
    from teatree.utils.editable_pth import (  # noqa: PLC0415 — deferred: keeps CLI startup light
        expected_checkout,
        receipt_editable_source,
        repair_receipt_to_checkout,
    )

    try:
        expected = expected_checkout()
        source = receipt_editable_source()
    except Exception as exc:  # noqa: BLE001 — an inspection failure warns and passes, never blocks doctor
        typer.echo(f"WARN  Could not inspect the t3 shim's uv receipt: {exc}")
        return True
    if expected is None or source is None or source.resolve() == expected:
        return True

    if repair and repair_receipt_to_checkout(expected):
        typer.echo(f"WARN  Re-pointed the t3 editable install at {expected} (uv receipt recorded {source}).")
        return True
    typer.echo(
        f"FAIL  The active t3 shim's uv receipt records an editable source {source} that does not "
        f"match the expected checkout {expected} — a relocated or same-name-hijacked editable install "
        f"is serving t3 from the wrong path. Re-point it: `t3 doctor check --repair` "
        f"(or `uv tool install --editable {expected} --force`)."
    )
    return False


def _check_editable_sanity() -> bool:
    from teatree.cli.doctor import DoctorService  # noqa: PLC0415 — deferred: breaks checks ↔ doctor cycle

    # A contribute/editable mismatch is an advisory WARN, not a hard FAIL — it is
    # surfacing-only and must not redden the run (the watchdog DM extracts only
    # FAIL lines, so a WARN-reddened run pages the owner with no detail). Only a
    # genuine crash gates the exit code (#3313).
    try:
        for problem in DoctorService.check_editable_sanity():
            typer.echo(f"WARN  {problem}")
    except Exception as exc:  # noqa: BLE001 — overlay loading can fail in many ways
        typer.echo(f"FAIL  Editable sanity check crashed: {exc.__class__.__name__}: {exc}")
        return False
    return True


def _check_skills() -> bool:
    ok = True
    claude_skills = Path.home() / ".claude" / "skills"
    if claude_skills.is_dir():
        from teatree.skill_support.schema import validate_directory  # noqa: PLC0415 — deferred: keeps CLI startup light

        errors, warnings = validate_directory(claude_skills)
        for warning in warnings:
            typer.echo(f"WARN  {warning}")
        for error in errors:
            typer.echo(f"FAIL  {error}")
            ok = False
        if not errors:
            skill_count = sum(1 for d in claude_skills.iterdir() if d.is_dir() and (d / "SKILL.md").is_file())
            typer.echo(f"OK    {skill_count} skill(s) validated")
    return ok


def _check_stale_uv_venv() -> bool:
    """Detect + clean an empty uv-built ``.venv`` in a Pipfile-managed clone (#2005).

    A clone carrying a ``Pipfile`` that also holds an in-project ``.venv`` built
    by uv with nothing installed is a wrong-toolchain artifact — it shadows
    pipenv's managed venvs and poisons both ``uv run`` and ``pipenv run``. Walks
    every repo the other repo-scoped doctor gates audit (:func:`_collect_repos`),
    removes each offending ``.venv``, and WARNs. A *successful* removal is a WARN
    that keeps the run GREEN (the problem is fixed; a WARN is surfacing-only and
    is not extracted into the watchdog's FAIL-line DM). A removal that FAILS is a
    hard FAIL — the poisoned venv persists, so it must not be silent success
    (#3313). Removal makes the next run a no-op (idempotent).
    """
    import shutil  # noqa: PLC0415 — deferred: loaded only when this command runs

    from teatree.cli.update import _collect_repos  # noqa: PLC0415 — deferred: keeps CLI startup light
    from teatree.utils.venv_artifacts import find_stale_uv_venv  # noqa: PLC0415 — deferred: keeps CLI startup light

    ok = True
    for _name, repo in _collect_repos():
        try:
            stale = find_stale_uv_venv(repo)
            if stale is None:
                continue
            shutil.rmtree(stale)
            typer.echo(
                f"WARN  Removed empty uv-built .venv shadowing pipenv in {repo} ({stale.name}). "
                "It poisoned both `uv run` and `pipenv run`; pipenv will rebuild its own venv."
            )
        except OSError as exc:
            typer.echo(
                f"FAIL  Could not remove empty uv-built .venv in {repo}: {exc}. "
                "Delete it manually (`rm -rf .venv`), then re-run `t3 doctor check`."
            )
            ok = False
    return ok


def _check_legacy_overlay_alias() -> None:
    """Warn (never rewrite) on a stale legacy alias entry in the DB overlays registry.

    souliane/teatree#1108: older ``slack-bot`` runs recorded a short overlay entry
    (e.g. ``teatree``) for an overlay whose canonical entry-point name is
    ``t3-<alias>``. Discovery now folds such a bare config-only alias entry into
    its canonical overlay so it is no longer listed twice — but the stale entry is
    confusing to read. Surface it as a WARN with the corrective rename; the
    agent/user does the edit (no auto-rewrite of the user's registry).
    """
    try:
        from importlib.metadata import entry_points  # noqa: PLC0415 — deferred: loaded only when this command runs

        from teatree.config import _match_canonical_ep, load_config  # noqa: PLC0415 — deferred: keeps CLI startup light

        config = load_config()
        ep_names = {ep.name for ep in entry_points(group="teatree.overlays")}
        for name, overlay_cfg in config.raw.get("overlays", {}).items():
            if name in ep_names or overlay_cfg.get("class") or overlay_cfg.get("path"):
                continue
            canonical = _match_canonical_ep(name, ep_names)
            if canonical is not None:
                typer.echo(
                    f"WARN  Stale overlay entry '{name}' in the DB overlays registry — "
                    f"the canonical overlay is '{canonical}'. Rename it to "
                    f"'{canonical}' (discovery folds it for now)."
                )
    except Exception:  # noqa: BLE001 — doctor warnings must never crash the run
        return


def _check_stale_path_t3(env: dict[str, str] | None = None) -> bool:
    import os  # noqa: PLC0415 — deferred: loaded only when this command runs

    resolved_env = env if env is not None else dict(os.environ)
    path_dirs = [Path(d) for d in resolved_env.get("PATH", "").split(os.pathsep) if d]
    home = Path(resolved_env.get("HOME", str(Path.home())))
    uv_tool_bin_dir_str = resolved_env.get("UV_TOOL_BIN_DIR")
    uv_bin_dir = Path(uv_tool_bin_dir_str) if uv_tool_bin_dir_str else home / ".local" / "bin"
    uv_bin_dir_resolved = uv_bin_dir.resolve()

    uv_pos = next(
        (i for i, d in enumerate(path_dirs) if d.resolve() == uv_bin_dir_resolved and (d / "t3").is_file()),
        None,
    )
    if uv_pos is None:
        return True

    shadows = [d / "t3" for i, d in enumerate(path_dirs) if i < uv_pos and (d / "t3").is_file()]
    if not shadows:
        return True

    uv_t3 = uv_bin_dir / "t3"
    for shadow in shadows:
        typer.echo(
            f"FAIL  Shadowing t3 at {shadow} precedes the uv-managed {uv_t3} on PATH. "
            f"This stale entry masks dep updates. Remove it: rm {shadow}",
        )
    return False


def _configured_review_skill_gaps() -> list[str]:
    """A FAIL line per configured review skill that resolves to no installed skill (#3352).

    Enumerates every registered overlay's effective ``architectural_review_skill``
    (only when the always-on cadence is not disabled for that overlay) and
    ``review_skill`` (only when the project opted in by setting it non-empty), then
    confirms each name resolves to an installed ``SKILL.md`` in the canonical skill
    set — the same enumeration :mod:`teatree.skill_support.ref_validator` uses for
    dangling references. A name that will actually be dispatched/gated but resolves
    to nothing is the exact ``ac-reviewing-skills`` → ``ac-reviewing-codebase``
    incident class, at the live config site rather than the ``.teatree-skills.yml`` /
    ``agents/*.md`` sites the reference validator already covers. Empty == clean.
    """
    from teatree.config import (  # noqa: PLC0415 — deferred: keeps CLI startup light
        discover_overlays,
        get_effective_settings,
    )
    from teatree.skill_support.ref_validator import (  # noqa: PLC0415 — deferred: keeps CLI startup light
        canonical_skill_names,
        default_search_dirs,
        resolves_to_canonical,
    )

    canonical = canonical_skill_names(default_search_dirs())
    overlay_names: list[str | None] = [entry.name for entry in discover_overlays()] or [None]
    gaps: list[str] = []
    for overlay_name in overlay_names:
        settings = get_effective_settings(overlay_name)
        configured: list[tuple[str, str]] = [("review_skill", settings.review_skill.strip())]
        if not settings.architectural_review_disabled:
            configured.append(("architectural_review_skill", settings.architectural_review_skill.strip()))
        scope = overlay_name or "(active overlay)"
        for label, skill in configured:
            if skill and not resolves_to_canonical(skill, canonical):
                gaps.append(
                    f"FAIL  Configured {label}={skill!r} (overlay {scope}) resolves to no installed skill — "
                    f"the review discipline that depends on it dispatches empty. Install it with "
                    f"`apm install souliane/skills/{skill}` (or re-run `t3 setup` with `apm` present), "
                    "then re-run `t3 doctor check`."
                )
    return gaps


def _check_configured_review_skills() -> bool:
    """FAIL when a configured review-skill name doesn't resolve to an installed skill (#3352).

    The gap :func:`_check_skills` leaves: it validates only skills that ARE present
    under ``~/.claude/skills`` and is silent when the directory is absent, so a
    ``review_skill`` / ``architectural_review_skill`` configured to a name no skill
    is installed for passes unseen — the review cadence and the reviewing-phase
    evidence gate then run against an unloadable skill with zero signal. This
    resolves the effective values and hard-FAILs loudly on any that dangle.

    Crash-proof: any resolution error degrades to a WARN so a doctor run never
    aborts on this check.
    """
    try:
        gaps = _configured_review_skill_gaps()
    except Exception as exc:  # noqa: BLE001 — a doctor check must never crash the run
        typer.echo(f"WARN  Configured-review-skill check crashed: {exc.__class__.__name__}: {exc}")
        return True
    for gap in gaps:
        typer.echo(gap)
    return not gaps
