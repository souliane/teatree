"""CLI surface for codex review (#1254).

``t3 codex review <pr_url>`` is the manual fire-and-forget counterpart
to :class:`CodexReviewScanner`: the same marker-claim + variant
classification logic, exposed for ad-hoc invocation. Useful when the
user wants to force a codex run on a PR the auto-dispatch scanner has
not yet picked up (e.g. a colleague's PR, a PR on a non-fleet overlay,
or a re-run on the same SHA after clearing the marker).

The CLI prints a JSON envelope identifying the variant and the
dispatch payload — the runtime layer reads that and spawns the agent
via the standard Task tool. Keeping the dispatch off the CLI's hot
path (CLI emits intent, runtime executes) mirrors the scanner →
dispatcher → runtime separation the rest of the loop follows.
"""

import json
import re
import sys

import typer

from teatree.utils.django_bootstrap import ensure_django

codex_app = typer.Typer(no_args_is_help=True, help="Auto-dispatch /codex:review surfaces.")


# Matches both ``https://github.com/owner/repo/pull/123`` and the
# alternative ``pulls/`` plural form gh occasionally emits.
_PR_URL_RE = re.compile(r"^https?://(?:[^/]+/)+(?P<slug>[^/]+/[^/]+)/pulls?/(?P<pr_id>\d+)/?$")


@codex_app.command("review")
def review(  # noqa: PLR0913, PLR0917 — typer command: every param is a CLI flag mapped 1:1 to the public ``codex review`` surface (pr_url/head-sha/path/overlay/force/json). The arg list IS the CLI contract.
    pr_url: str = typer.Argument(..., help="PR URL, e.g. https://github.com/owner/repo/pull/123"),
    head_sha: str = typer.Option(..., "--head-sha", help="Current head SHA of the PR."),
    changed_paths: list[str] = typer.Option(
        [],
        "--path",
        help="Changed file path (repeatable) — used to pick standard vs adversarial variant.",
    ),
    overlay: str = typer.Option("", "--overlay", help="Overlay name to tag the marker with."),
    force: bool = typer.Option(False, "--force", help="Re-dispatch even when a marker exists for this SHA."),  # noqa: FBT001 — typer boolean flag, not a positional bool foot-gun.
    output_json: bool = typer.Option(True, "--json/--no-json", help="Emit machine-readable JSON envelope."),  # noqa: FBT001 — typer boolean flag.
) -> None:
    """Emit a codex-review dispatch envelope for *pr_url* at *head_sha*.

    Records a :class:`CodexReviewMarker` so the loop scanner won't
    re-dispatch the same SHA. Prints a JSON envelope the runtime can
    use to spawn the codex agent.
    """
    parsed = _PR_URL_RE.match(pr_url)
    if parsed is None:
        typer.echo(f"error: malformed PR URL: {pr_url!r}", err=True)
        raise typer.Exit(code=2)
    ensure_django()
    slug = parsed.group("slug")
    pr_id = int(parsed.group("pr_id"))
    variant = _classify_variant_cli(tuple(changed_paths))
    from teatree.core.models.codex_review_marker import CodexReviewMarker  # noqa: PLC0415

    if force:
        CodexReviewMarker.objects.filter(slug=slug, pr_id=pr_id, head_sha=head_sha).delete()
    marker = CodexReviewMarker.claim(
        slug=slug,
        pr_id=pr_id,
        head_sha=head_sha,
        overlay=overlay,
        variant=variant,
    )
    envelope = {
        "dispatched": marker is not None,
        "slug": slug,
        "pr_id": pr_id,
        "head_sha": head_sha,
        "pr_url": pr_url,
        "variant": variant,
        "overlay": overlay,
        "reason": "claimed" if marker is not None else "already_dispatched",
    }
    if output_json:
        json.dump(envelope, sys.stdout)
        sys.stdout.write("\n")
    else:
        status = "dispatched" if marker is not None else "skipped"
        typer.echo(f"{status}: {slug}#{pr_id}@{head_sha[:8]} → /{variant}")


def _classify_variant_cli(changed_files: tuple[str, ...]) -> str:
    """Same classifier as the scanner — kept in lockstep via shared markers."""
    from teatree.loop.scanners.codex_review import (  # noqa: PLC0415
        ADVERSARIAL_PATH_MARKERS,
        ADVERSARIAL_REVIEW_VARIANT,
        STANDARD_REVIEW_VARIANT,
    )

    for path in changed_files:
        lowered = path.lower()
        if any(marker in lowered for marker in ADVERSARIAL_PATH_MARKERS):
            return ADVERSARIAL_REVIEW_VARIANT
    return STANDARD_REVIEW_VARIANT


__all__ = ["codex_app"]
