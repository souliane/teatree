"""Shared self-update mechanics: editable reinstall + runtime self-DB migrate.

These primitives re-anchor the *running* interpreter on freshly-pulled
teatree source. A ``git pull`` alone leaves the live process importing the
old modules; re-anchoring needs ``uv tool install --editable <src>
--reinstall`` + ``t3 setup`` + a non-destructive self-DB migrate.

This module is the single home for that logic so the two callers run the
*exact* same sequence:

* the ``t3 update`` CLI flow (:mod:`teatree.cli.update`), and
* the loop's deferred-reinstall drain (:mod:`teatree.loop.self_update_reinstall`).

It deliberately sits below both ``teatree.cli`` and ``teatree.loop`` in the
module graph (it depends only on ``teatree.utils``) so the loop can re-anchor
without importing the CLI — the dependency the tach graph forbids
(``teatree.loop`` must not depend on ``teatree.cli``).
"""

import os
import shutil
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import typer

from teatree.utils.run import CompletedProcess, run_allowed_to_fail

type SubprocessRunner = Callable[..., CompletedProcess[str]]


def current_editable_source(uv_bin: str) -> Path | None:
    """Return the editable source recorded in uv's teatree tool receipt, or None.

    Returns None when teatree isn't installed as a uv tool, when it's installed
    non-editable (regular PyPI-style install), or when the receipt is
    unparsable.  ``~/.local/share/uv/tools/teatree/uv-receipt.toml`` looks like::

        [tool]
        requirements = [{ name = "teatree", editable = "/path/to/clone" }]
    """
    result = run_allowed_to_fail([uv_bin, "tool", "dir"], expected_codes=None)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    receipt = Path(result.stdout.strip()) / "teatree" / "uv-receipt.toml"
    if not receipt.is_file():
        return None
    try:
        data = tomllib.loads(receipt.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError:
        return None
    for req in data.get("tool", {}).get("requirements", []):
        if req.get("name") == "teatree":
            editable = req.get("editable")
            return Path(editable) if editable else None
    return None


@dataclass
class ReinstallResult:
    """Outcome of re-anchoring the running editable install + re-running setup.

    ``ok`` is true only when both the editable reinstall and ``t3 setup``
    completed (or were validly skipped — no ``uv`` on PATH leaves the
    install untouched, which is not a failure here). ``error`` carries the
    first non-empty diagnostic for the caller to surface.
    """

    ok: bool
    reinstalled: bool
    error: str = ""


def reinstall_running_editable(*, runner: SubprocessRunner = run_allowed_to_fail) -> ReinstallResult:
    """Reinstall the running editable teatree source, then re-run ``t3 setup``.

    Shared by the ``t3 update`` CLI flow and the loop's deferred-reinstall
    drain (:mod:`teatree.loop.self_update_reinstall`) so both run the exact
    same ``uv tool install --editable <src> --reinstall`` + ``t3 setup``
    sequence. ``runner`` is injectable for tests — production passes the
    audited :func:`teatree.utils.run.run_allowed_to_fail`.
    """
    reinstalled = False
    errors: list[str] = []
    uv_bin = shutil.which("uv")
    if uv_bin:
        source = current_editable_source(uv_bin)
        if source is not None and source.is_dir():
            result = runner(
                [uv_bin, "tool", "install", "--editable", str(source), "--reinstall"],
                expected_codes=None,
            )
            if result.returncode != 0:
                errors.append(f"reinstall: {result.stderr.strip()}")
            else:
                reinstalled = True

    t3_bin = shutil.which("t3") or sys.argv[0]
    setup = runner([t3_bin, "setup"], expected_codes=None)
    if setup.returncode != 0:
        errors.append(f"setup: {setup.stderr.strip()}")
    return ReinstallResult(ok=not errors, reinstalled=reinstalled, error="; ".join(errors))


def _self_db_migrate_env() -> dict[str, str]:
    """Env for the runtime-interpreter self-DB migrate.

    Strips an inherited ``DJANGO_SETTINGS_MODULE`` (a worktree-specific value
    leaking from the caller would crash the subprocess with
    ``ModuleNotFoundError`` — the #959 class) and pins ``teatree.settings``,
    so the migrate always targets the teatree-core control DB the runtime
    ``t3`` resolves.
    """
    env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
    env["DJANGO_SETTINGS_MODULE"] = "teatree.settings"
    return env


def _self_db_migrate_cmd(*args: str) -> list[str]:
    """``python -m teatree <args>`` using the *running* interpreter.

    Running in the runtime process — not ``uv --directory <clone>`` — is the
    #126 fix: a worktree-anchored editable install resolves its control DB by
    the *code's* on-disk location, so ``uv --directory <clone>`` auto-isolates
    onto a sibling DB the runtime never reads, while ``python -m teatree``
    (this interpreter, this installed package) resolves the exact DB the merge
    gate inspects.
    """
    return [sys.executable, "-m", "teatree", *args]


def _self_db_has_pending_migrations() -> bool:
    """Probe whether the runtime teatree self-DB has unapplied migrations.

    Runs ``python -m teatree migrate --check --no-input`` in the runtime
    interpreter: Django exits 0 when the DB is fully migrated and non-zero
    when migrations are pending. This decouples "should we migrate?" from
    "did a repo advance *this run*?" — an interrupted prior ``t3 update`` or
    an out-of-band ``git pull`` can leave the SHA already current with a
    stale self-DB (#929), so the per-run ``UPDATED`` flag is the wrong gate.
    """
    result = run_allowed_to_fail(
        _self_db_migrate_cmd("migrate", "--check", "--no-input"),
        env=_self_db_migrate_env(),
        expected_codes=None,
    )
    return result.returncode != 0


def _migrate_self_db() -> None:
    """Apply pending teatree self-DB migrations non-destructively, in-process.

    A teatree git-pull can land new migrations; the self-update path must
    apply them or the sanctioned merge path breaks against the now-stale
    self-DB. Runs ``python -m teatree migrate --no-input`` in the *running*
    interpreter so the DB it migrates is exactly the one the runtime ``t3``
    (and the merge gate) resolves — never a ``uv --directory <clone>``
    sibling DB (#126). Non-destructive: live ticket/session/lease state is
    preserved.

    A failure is **fail-closed** (#929): it raises ``typer.Exit(code=1)``
    rather than swallowing a WARN, so the caller can never exit 0 with a
    half-migrated self-DB and silently break #870's
    fail-closed-on-unmigrated-self-DB guarantee.
    """
    typer.echo("Applying teatree self-DB migrations (non-destructive, runtime self-DB) ...")
    result = run_allowed_to_fail(
        _self_db_migrate_cmd("migrate", "--no-input"),
        env=_self_db_migrate_env(),
        expected_codes=None,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        typer.echo("")
        typer.echo(f"!! FAIL: self-DB migration failed — {detail}")
        typer.echo("!! The teatree self-DB is left UNMIGRATED; the sanctioned merge path (#870) will fail closed.")
        typer.echo("!! Resolve the migration error and re-run `t3 update` before relying on the merge path.")
        typer.echo("")
        raise typer.Exit(code=1)
    typer.echo("OK    self-DB migrations applied.")


def ensure_self_db_migrated(*, quiet: bool = False) -> bool:
    """Migrate the runtime teatree self-DB iff migrations are actually pending.

    Probe-gated and fully decoupled from whether a repo advanced *this
    run* (#929): an interrupted prior ``t3 update`` or an out-of-band
    ``git pull`` leaves the SHA current with a stale self-DB, and the
    migration must still run.  Returns ``True`` when the self-DB is left
    unmigrated (caller exits non-zero — fail-closed, #870); ``False``
    when nothing was pending or the migration succeeded.

    With *quiet* the no-op case (nothing pending) emits nothing, so a
    caller like ``t3 setup`` stays silent on a current DB; the migrate and
    fail-closed paths always report regardless of *quiet*.

    Both probe and migrate run in the runtime interpreter (``python -m
    teatree``), so they always target the DB the runtime resolves — there
    is no clone to resolve and no ``uv`` dependency for this path (#126).
    """
    if not _self_db_has_pending_migrations():
        if not quiet:
            typer.echo("OK    self-DB already migrated.")
        return False
    try:
        _migrate_self_db()
    except typer.Exit:
        return True
    return False


def seed_db_config_from_toml() -> None:
    """Seed the DB config store from ``~/.teatree.toml`` — the #938 auto-migration (TODO-75).

    Runs ``python -m teatree config_setting import --no-clobber`` in the *running*
    interpreter so it targets the exact self-DB the runtime ``t3`` resolves (the
    #126 rule the self-DB migrate follows), with the same stripped
    ``DJANGO_SETTINGS_MODULE`` env (#959). ``--no-clobber`` is load-bearing:
    ``t3 setup`` runs on every update, so the migration must only seed keys absent
    from the store and never overwrite a value the user has since changed via
    ``config_setting set``.

    Best-effort, NOT fail-closed: a failure is a single WARN and the function
    returns. The TOML remains readable and the dual-read resolver falls through to
    the dataclass default, so a failed config seed must not abort ``t3 setup`` —
    unlike the self-DB migrate, which is fail-closed because the merge gate (#870)
    depends on it.
    """
    result = run_allowed_to_fail(
        _self_db_migrate_cmd("config_setting", "import", "--no-clobber"),
        env=_self_db_migrate_env(),
        expected_codes=None,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "(no output)"
        typer.echo(f"WARN  config DB seed skipped — {detail}")
        return
    summary = result.stdout.strip().splitlines()
    if summary:
        typer.echo(f"OK    config DB store: {summary[-1].strip()}")


def seed_default_loops() -> None:
    """Seed the default loops + prompts into the self-DB — the #2513 install seed.

    Runs ``python -m teatree seed_loops`` in the *running* interpreter so it
    targets the exact self-DB the runtime ``t3`` resolves (the #126 rule the
    self-DB migrate follows), with the same stripped ``DJANGO_SETTINGS_MODULE``
    env (#959). The ``seed_loops`` command is idempotent — it ``get_or_create``s
    by name, so re-running ``t3 setup`` creates nothing new and never clobbers an
    operator-edited row.

    Best-effort, NOT fail-closed: a failure is a single WARN and the function
    returns. A missing default loop is recoverable (the operator can re-run the
    seed or add rows in the admin), so a failed seed must not abort ``t3 setup``.
    """
    result = run_allowed_to_fail(
        _self_db_migrate_cmd("seed_loops"),
        env=_self_db_migrate_env(),
        expected_codes=None,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "(no output)"
        typer.echo(f"WARN  default loop seed skipped — {detail}")
        return
    summary = result.stdout.strip().splitlines()
    if summary:
        typer.echo(f"OK    default loops: {summary[-1].strip()}")


__all__ = [
    "ReinstallResult",
    "SubprocessRunner",
    "current_editable_source",
    "ensure_self_db_migrated",
    "reinstall_running_editable",
    "seed_db_config_from_toml",
    "seed_default_loops",
]
