"""Infra CLI — manage teatree-wide services (shared Redis, etc.)."""

import typer

from teatree.utils import redis_container

infra_app = typer.Typer(no_args_is_help=True, help="Teatree-wide infrastructure services.")

redis_app = typer.Typer(no_args_is_help=True, help="Shared Redis container (teatree-redis).")
infra_app.add_typer(redis_app, name="redis")


@redis_app.command(name="up")
def redis_up() -> None:
    """Start the shared Redis container (idempotent)."""
    redis_container.ensure_running()
    typer.echo(f"{redis_container.CONTAINER_NAME}: {redis_container.status()}")


@redis_app.command(name="down")
def redis_down() -> None:
    """Stop the shared Redis container."""
    redis_container.stop()
    typer.echo(f"{redis_container.CONTAINER_NAME}: {redis_container.status()}")


@redis_app.command(name="status")
def redis_status() -> None:
    """Print the shared Redis container status."""
    typer.echo(f"{redis_container.CONTAINER_NAME}: {redis_container.status()}")
