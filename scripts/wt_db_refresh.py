#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#   "typer>=0.12",
# ]
# ///
"""Refresh worktree DB from DSLR snapshot or dump.

Usage: wt_db_refresh [--force] [variant]

Without --force: tries DSLR restore first (fast), then full reimport.
With --force: drops the existing DB first, then reimports from scratch
    using the full fallback chain (DSLR dev copy → local DSLR
    snapshot → .data/ dump → CI dump).
"""

import subprocess
import sys
from datetime import UTC, datetime

import lib.init
import typer

lib.init.init()

from lib.db import db_exists, pg_env, pg_host, pg_user
from lib.env import read_env_key, resolve_context
from lib.registry import call as ext


def wt_db_refresh(variant: str = "", *, force: bool = False) -> int:
    try:
        ctx = resolve_context()
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    variant = ext("wt_detect_variant", variant)
    if not variant:
        for envwt_path in (
            f"{ctx.ticket_dir}/.env.worktree",
            f"{ctx.wt_dir}/.env.worktree",
        ):
            variant = read_env_key(envwt_path, "WT_VARIANT")
            if variant:
                break

    db_name = f"wt_{ctx.ticket_number}"
    if variant:
        db_name += f"_{variant}"

    today = datetime.now(tz=UTC).strftime("%Y%m%d")
    dslr_suffix = f"-{variant}" if variant else ""
    dslr_name = f"{today}_development{dslr_suffix}"

    if force:
        print(f"Force-dropping DB '{db_name}'...")
        env = pg_env()
        host = pg_host()
        user = pg_user()
        subprocess.run(
            ["dropdb", "-h", host, "-U", user, "--if-exists", db_name],
            env=env,
            check=False,
        )

    # Without --force (or after drop): try fast DSLR restore first
    if not force and db_exists(db_name):
        # DB exists and no --force: just restore from DSLR on top
        result = subprocess.run(
            ["dslr", "restore", dslr_name],
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            print("DB restored from DSLR snapshot (fast)")
            ext("wt_post_db", ctx.wt_dir)
            return 0

    # Full reimport via the 4-strategy fallback chain
    if ext("wt_db_import", db_name, variant, ctx.main_repo):
        print("DB reimported")
    else:
        print("No DSLR snapshot and no import available.", file=sys.stderr)
        return 1

    ext("wt_post_db", ctx.wt_dir)

    # Take DSLR snapshot for next time
    subprocess.run(
        ["dslr", "snapshot", "-y", dslr_name],
        capture_output=True,
        check=False,
    )

    print("DB refresh complete")
    return 0


app = typer.Typer(add_completion=False)


@app.command()
def main(
    variant: str = typer.Argument("", help="Tenant variant (e.g. production, staging)"),
    force: bool = typer.Option(False, "--force", help="Drop existing DB before reimport"),
) -> None:
    sys.exit(wt_db_refresh(variant, force=force))


if __name__ == "__main__":
    app()
