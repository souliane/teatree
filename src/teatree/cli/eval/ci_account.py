"""``t3 eval ci-account`` — inspect and switch the account CI's OAuth secret holds.

``show`` reports which account the ``CLAUDE_CODE_OAUTH_TOKEN`` repo secret currently
holds (the cost basis a benchmark's shards must be attributed to — cost figures are not
comparable across accounts on different plans) alongside every configured account's
headroom. ``switch`` points the secret at the healthiest one, or exits non-zero naming
every rejection when none can serve the run.

Both delegate the selection and the rotation to
:mod:`teatree.ci_oauth_switch`; this module is the typer surface only. No token value
is ever rendered — an account is always identified by its ``pass`` entry.
"""

import datetime as dt
import json

import typer

from teatree.backends.github.ci_eval_client import DEFAULT_CI_EVAL_REPO
from teatree.utils.django_bootstrap import ensure_django

ci_account_app = typer.Typer(help="Inspect / switch the Anthropic account CI's OAuth secret holds.")

_REPO_OPTION = typer.Option(DEFAULT_CI_EVAL_REPO, "--repo", help="The repo whose Actions secret to read/write.")
_JSON_OPTION = typer.Option(False, "--json", help="Emit the report as JSON.")
_STARTING_IN_OPTION = typer.Option(
    0,
    "--starting-in",
    help=(
        "Minutes until the run starts. A 5h window that resets before then counts as fully "
        "free, so an account can be scored for a run scheduled after its reset."
    ),
)


def _rows() -> list:
    """Every configured account's health row — the same data ``t3 tokens`` renders."""
    from teatree.token_report import TokenReport  # noqa: PLC0415 — deferred: needs Django

    return TokenReport().rows()


def _switcher(repo: str):  # noqa: ANN202 — the concrete type needs the deferred Django import
    from teatree.ci_oauth_switch import CiAccountSwitcher  # noqa: PLC0415 — deferred: needs Django

    return CiAccountSwitcher(repo=repo)


@ci_account_app.command("show")
def show(*, repo: str = _REPO_OPTION, json_output: bool = _JSON_OPTION) -> None:
    """Report which account CI's OAuth secret holds, and every account's headroom."""
    ensure_django()

    from teatree.ci_oauth_switch import select_account  # noqa: PLC0415 — deferred: needs Django

    now = dt.datetime.now(dt.UTC)
    selection = select_account(_rows(), run_start=now)
    active = _switcher(repo).active_account()
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "repo": repo,
                    "active_account": active,
                    "eligible": [
                        {
                            "account": entry.account,
                            "headroom_5h": entry.headroom_5h,
                            "headroom_7d": entry.headroom_7d,
                            "binding_headroom": entry.binding_headroom,
                        }
                        for entry in selection.ranked
                    ],
                    "rejected": [
                        {"account": rejection.account, "reason": rejection.reason} for rejection in selection.rejected
                    ],
                },
                indent=2,
            )
        )
        return
    typer.echo(f"repo:   {repo}")
    typer.echo(f"active: {active or '(unrecorded — the secret predates account tracking)'}")
    for entry in selection.ranked:
        typer.echo(
            f"  {entry.account}  5h free {entry.headroom_5h:.0%}  weekly free {entry.headroom_7d:.0%}  "
            f"binding {entry.binding_headroom:.0%}"
        )
    for rejection in selection.rejected:
        typer.echo(f"  REJECTED {rejection}")


@ci_account_app.command("switch")
def switch(
    *,
    repo: str = _REPO_OPTION,
    json_output: bool = _JSON_OPTION,
    starting_in: int = _STARTING_IN_OPTION,
    dry_run: bool = typer.Option(False, "--dry-run", help="Report the switch without writing anything."),
) -> None:
    """Point CI's OAuth secret at the healthiest account; exit 1 when none can serve a run."""
    ensure_django()

    from teatree.ci_oauth_switch import NoEligibleAccountError  # noqa: PLC0415 — deferred: needs Django

    run_start = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=starting_in)
    try:
        outcome = _switcher(repo).switch(_rows(), run_start=run_start, dry_run=dry_run)
    except NoEligibleAccountError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=1) from error

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "repo": repo,
                    "account": outcome.account,
                    "previous": outcome.previous,
                    "changed": outcome.changed,
                    "applied": outcome.applied,
                    "binding_headroom": outcome.binding_headroom,
                    "headroom_5h": outcome.headroom_5h,
                    "headroom_7d": outcome.headroom_7d,
                    "rejected": [
                        {"account": rejection.account, "reason": rejection.reason} for rejection in outcome.rejected
                    ],
                },
                indent=2,
            )
        )
        return
    if not outcome.changed:
        typer.echo(f"no-op: {repo} already runs on {outcome.account} (the healthiest configured account)")
        return
    verb = "would switch" if dry_run else "switched"
    typer.echo(f"{verb} {repo} from {outcome.previous or '(unrecorded)'} to {outcome.account}")
