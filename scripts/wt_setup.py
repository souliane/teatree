#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#   "typer>=0.12",
# ]
# ///
"""Full worktree setup: symlinks + .env.worktree + DB provisioning + DSLR snapshot.

Usage: wt_setup [variant] [ticket_url]
Run from inside a worktree ($T3_WORKSPACE_DIR/<branch-name>/<repo>).
"""

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import lib.init
import typer

lib.init.init()

from lib.db import db_exists, worktree_db_name
from lib.env import WorktreeContext, find_free_ports, resolve_context
from lib.registry import call as ext


def compute_compose_project_name(repo_name: str, ticket_number: str) -> str:
    """Compute a unique COMPOSE_PROJECT_NAME for a worktree.

    Example: my-backend worktree for ticket 1234 → "my-backend-wt1234"
    """
    return f"{repo_name}-wt{ticket_number}"


@dataclass(frozen=True)
class _SetupConfig:
    variant: str
    ticket_url: str
    db_name: str
    backend_port: int
    frontend_port: int
    postgres_port: int
    redis_port: int
    compose_project_name: str


def _resolve_setup_context() -> WorktreeContext | None:
    try:
        return resolve_context()
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return None


def _print_setup_summary(ctx: WorktreeContext, cfg: _SetupConfig) -> None:
    print("=== Worktree Setup ===")
    print(f"  Ticket:        {ctx.ticket_number}")
    if cfg.variant:
        print(f"  Variant:       {cfg.variant}")
    print(f"  DB:            {cfg.db_name}")
    print(f"  Backend port:  {cfg.backend_port}")
    print(f"  Frontend port: {cfg.frontend_port}")
    print(f"  Postgres port: {cfg.postgres_port}")
    print(f"  Redis port:    {cfg.redis_port}")
    print(f"  Compose:       {cfg.compose_project_name}")


def _write_env_worktree(ctx: WorktreeContext, cfg: _SetupConfig) -> str:
    envfile = str(Path(ctx.ticket_dir) / ".env.worktree")
    with Path(envfile).open("w", encoding="utf-8") as f:
        f.write(f"WT_VARIANT={cfg.variant}\n")
        f.write(f"TICKET_DIR={ctx.ticket_dir}\n")
        f.write(f"TICKET_URL={cfg.ticket_url}\n")
        f.write(f"WT_DB_NAME={cfg.db_name}\n")
        f.write(f"BACKEND_PORT={cfg.backend_port}\n")
        f.write(f"FRONTEND_PORT={cfg.frontend_port}\n")
        f.write(f"POSTGRES_PORT={cfg.postgres_port}\n")
        f.write(f"REDIS_PORT={cfg.redis_port}\n")
        f.write(f"BACK_END_URL=http://localhost:{cfg.backend_port}\n")
        f.write(f"FRONT_END_URL=http://localhost:{cfg.frontend_port}\n")
        f.write(f"COMPOSE_PROJECT_NAME={cfg.compose_project_name}\n")
    return envfile


def _link_repo_env_worktree(ctx: WorktreeContext) -> None:
    wt_envwt = Path(ctx.wt_dir) / ".env.worktree"
    ticket_envwt = Path(ctx.ticket_dir) / ".env.worktree"
    if wt_envwt.is_symlink() or wt_envwt.is_file():
        wt_envwt.unlink()
    wt_envwt.symlink_to(ticket_envwt)


def _provision_database(ctx: WorktreeContext, db_name: str, variant: str) -> bool:
    ext("wt_services", ctx.main_repo, ctx.wt_dir)
    if db_exists(db_name):
        print(f"  DB '{db_name}' already exists — skipping restore")
        return True
    if ext("wt_db_import", db_name, variant, ctx.main_repo):
        print("  DB imported")
        return True
    print("  WARNING: No DB import available.")
    print("  Skipping DB provisioning.")
    return False


def wt_setup(variant: str = "", ticket_url: str = "") -> int:
    ctx = _resolve_setup_context()
    if ctx is None:
        return 1

    # Determine variant
    variant = ext("wt_detect_variant", variant)

    # Auto-detect free ports
    backend_port, frontend_port, postgres_port, redis_port = find_free_ports(ctx.ticket_dir)
    db_name = worktree_db_name(ctx.ticket_number, variant)

    # Compute COMPOSE_PROJECT_NAME
    compose_project_name = compute_compose_project_name(
        ctx.repo_name,
        ctx.ticket_number,
    )
    cfg = _SetupConfig(
        variant=variant,
        ticket_url=ticket_url,
        db_name=db_name,
        backend_port=backend_port,
        frontend_port=frontend_port,
        postgres_port=postgres_port,
        redis_port=redis_port,
        compose_project_name=compose_project_name,
    )

    _print_setup_summary(ctx, cfg)

    # Set in env so subprocess (Docker compose, pg CLI) inherits it
    os.environ["COMPOSE_PROJECT_NAME"] = compose_project_name
    os.environ["POSTGRES_PORT"] = str(postgres_port)
    os.environ.setdefault("POSTGRES_HOST", "localhost")
    os.environ.setdefault("POSTGRES_USER", "local_superuser")
    os.environ.setdefault("POSTGRES_PASSWORD", "local_superpassword")

    # --- Phase 1: Symlinks ---
    print("--- Phase 1: Symlinks ---")
    ext("wt_symlinks", ctx.wt_dir, ctx.main_repo, variant)

    # --- Phase 2: .env.worktree ---
    print("--- Phase 2: Environment ---")

    envfile = _write_env_worktree(ctx, cfg)

    # Let project skill / framework append extra vars
    ext("wt_env_extra", envfile)

    # Symlink repo-level .env.worktree → ticket-level .env.worktree so that
    # docker-compose.override.yml (env_file: .env.worktree) can find it.
    _link_repo_env_worktree(ctx)

    # Allow direnv and reload
    subprocess.run(["direnv", "allow", ctx.wt_dir], capture_output=True, check=False)

    print(f"  .env.worktree generated at {ctx.ticket_dir}/")

    # --- Phase 2b: Database Provisioning ---
    print("--- Database Provisioning ---")

    db_provisioned = _provision_database(ctx, db_name, variant)

    # Post-DB setup (migrations, superuser, etc.)
    if db_provisioned:
        ext("wt_post_db", ctx.wt_dir)
        print("  DB ready")
    else:
        print("  Skipping post-DB setup (no DB provisioned)")

    print("=== Setup complete ===")
    print()
    start_cmd = "start_session"
    if variant:
        start_cmd += f" {variant}"
    print(f"Start dev servers:  {start_cmd}")
    return 0


app = typer.Typer(add_completion=False)


@app.command()
def main(
    variant: str = typer.Argument("", help="Tenant variant (e.g. production, staging)"),
    ticket_url: str = typer.Argument("", help="Ticket URL (falls back to $TICKET_URL env var)"),
) -> None:
    ticket_url = ticket_url or os.environ.get("TICKET_URL", "")
    sys.exit(wt_setup(variant, ticket_url))


if __name__ == "__main__":
    app()
