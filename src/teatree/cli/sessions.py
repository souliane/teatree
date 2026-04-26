"""``t3 sessions`` — list recent Claude conversation sessions."""

from datetime import UTC

import typer

_MILLISECOND_TIMESTAMP_THRESHOLD = 1e12
_SECONDS_PER_HOUR = 3600
_SECONDS_PER_DAY = 86400
_PROMPT_DISPLAY_MAX = 80
_PROMPT_DISPLAY_TRUNCATE = 77


def _format_session_age(raw_ts: float | str, now: float) -> str:
    if isinstance(raw_ts, str):
        try:
            raw_ts = float(raw_ts)
        except (ValueError, TypeError):
            raw_ts = 0
    ts = raw_ts / 1000 if raw_ts > _MILLISECOND_TIMESTAMP_THRESHOLD else raw_ts
    if not ts:
        return "?"
    age_s = now - ts
    if age_s < _SECONDS_PER_HOUR:
        return f"{int(age_s / 60)}m ago"
    if age_s < _SECONDS_PER_DAY:
        return f"{int(age_s / _SECONDS_PER_HOUR)}h ago"
    return f"{int(age_s / _SECONDS_PER_DAY)}d ago"


def sessions(
    project: str = typer.Option("", help="Filter by project dir substring"),
    limit: int = typer.Option(20, "--limit", "-n", help="Max sessions to show"),
    *,
    all_projects: bool = typer.Option(False, "--all", "-a", help="Show sessions from all projects"),
) -> None:
    """List recent Claude conversation sessions with resume commands.

    By default shows sessions for the current working directory.
    Use --all to show sessions across all projects.
    """
    from datetime import datetime  # noqa: PLC0415

    from teatree.claude_sessions import SessionQuery, list_sessions  # noqa: PLC0415

    results = list_sessions(
        SessionQuery(
            project_filter=project,
            all_projects=all_projects,
            limit=limit,
        ),
    )

    if not results:
        typer.echo("No sessions found.")
        raise typer.Exit

    now = datetime.now(tz=UTC).timestamp()
    for r in results:
        age = _format_session_age(r.timestamp, now)
        prompt = r.first_prompt.replace("\n", " ").strip()
        if len(prompt) > _PROMPT_DISPLAY_MAX:
            prompt = prompt[:_PROMPT_DISPLAY_TRUNCATE] + "..."

        status_label = "done" if r.status == "finished" else r.status

        typer.echo(f"\n  {age:<8} [{status_label}] {r.project}")
        if prompt:
            typer.echo(f"           {prompt}")
        if r.status != "finished":
            resume = f"claude --resume {r.session_id}"
            typer.echo(f"           {f'cd {r.cwd} && {resume}' if r.cwd else resume}")

    typer.echo("")
