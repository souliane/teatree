"""Doctor self-heal — keep the Slack app-config token pair inside its 12-hour lifetime.

Slack expires this pair 12 hours after issue and mints both halves together, so
once it lapses it cannot be refreshed at all — the refresh token is as dead as
the access token it would have replaced. Keeping it alive is therefore a matter
of rotating on a cadence comfortably inside that window.

``t3 doctor`` is the right home for that, and the only one needed. It runs at
SessionStart (``hooks/scripts/bootstrap-cli.sh``) and, on the deployed stack, every
five minutes from the watchdog — two independent triggers, either of which is
well inside 12 hours. So the owner opening a session is enough to bring the
credential back inside its window even after the worker has been down for a day,
with no prompt and nothing to paste. That is the standing rule — the doctor
auto-fixes Slack tokens silently — expressed for the one Slack credential that
expires. Deliberately NOT on the loop tick: the tick would add a credential read
and a network call to the hottest path in the factory to buy a cadence the
watchdog already provides.

Surfacing-only by design, with one exception. The app-config token authorises
``apps.manifest.export`` / ``apps.manifest.update`` and nothing else: message
delivery runs on the per-overlay bot (``xoxb-``) token and Socket Mode on the
app-level (``xapp-``) token, neither of which expires. A dead app-config token
therefore blocks manifest edits during ``t3 setup`` and must never redden a box
whose factory is running fine — Slack is optional and must not become mandatory.
The exceptions are the two STORE faults, which are teatree's own and not Slack's.
A persistence failure means teatree rotated successfully and then lost the
credential Slack had just handed it. A write-ahead failure
(``STORE_UNWRITABLE``) means the ``pass`` store cannot round-trip at all, so the
rotation was refused: nothing was spent, but nothing CAN be rotated either, and
a credential that cannot be rotated is dead within 12 hours and re-mintable only
by hand. Both are genuine reds — a store teatree cannot write is not the
"Slack is optional" case, it is teatree's own credential plane being broken.
"""

import typer

from teatree.cli.slack.config_token import RotationOutcome, SlackConfigTokenPersistError, ensure_fresh_config_token
from teatree.cli.slack.provision import _slack_overlays


def check_slack_config_token_fresh() -> bool:
    """AUTO-ROTATE the Slack app-config pair when it nears expiry; silent when nothing was due.

    Follows the ``_check_dead_owner_lease`` shape: no ``--repair`` gate (the
    whole point is that it heals on a plain ``t3 doctor``), the mutation lives in
    :func:`~teatree.cli.slack.config_token.ensure_fresh_config_token` rather than
    here, and a rotation announces itself as ``WARN`` because state changed.
    Returns ``True`` for every outcome except the two store faults — a lost write
    (:class:`SlackConfigTokenPersistError`) and a store that failed its
    write-ahead round-trip (:data:`RotationOutcome.STORE_UNWRITABLE`). The latter
    reported NOTHING and passed before, which is the worst of the two failure
    shapes: the pair is intact but frozen, so a silent green here is a green that
    holds right up to the moment the credential expires for good.

    Gated on a Slack-backed overlay actually being registered, the same way the
    rest of the Slack doctor family is: a box with no Slack overlay has no
    manifest to edit, so it must not read a credential or reach the network on
    every doctor run.
    """
    try:
        if not _slack_overlays():
            return True
        report = ensure_fresh_config_token()
    except SlackConfigTokenPersistError as exc:
        typer.echo(f"FAIL  Slack app-config token rotated but could not be stored: {exc}")
        return False
    except Exception as exc:  # noqa: BLE001 — a self-heal probe must never crash the doctor run
        typer.echo(f"WARN  Slack app-config token check crashed: {exc.__class__.__name__}: {exc}")
        return True

    if report.outcome is RotationOutcome.STORE_UNWRITABLE:
        typer.echo(f"FAIL  Slack app-config token CANNOT be rotated — the store is unwritable: {report.detail}")
        return False
    if report.outcome is RotationOutcome.ROTATED:
        typer.echo(f"WARN  Auto-rotated the Slack app-config token pair — {report.detail}.")
    elif report.outcome is RotationOutcome.UNRECOVERABLE:
        typer.echo(f"WARN  Slack app-config token is unusable and cannot self-heal: {report.detail}.")
    return True


__all__ = ["check_slack_config_token_fresh"]
