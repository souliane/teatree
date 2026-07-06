"""``t3 <overlay> recipe score|approve`` — the read seam over the factory score (SIG-PR-2).

``score`` computes the recipe-weighted aggregate over the trailing window and
prints it. It COMPUTES read-only unconditionally (calibrating the recipe against
real ledger data is exactly why the flag ships OFF), but ``--record`` — the only
path that writes a :class:`~teatree.core.models.factory_score_snapshot.FactoryScoreSnapshot`
row or queues a :class:`~teatree.core.models.deferred_question.DeferredQuestion` —
refuses unless ``factory_score_enabled`` is on. So with the shipped defaults the
DB stays empty and no human is pinged: the flag-gated OFF footprint is just the
migrated (empty) table.

``approve`` is the human EVOLVE gate: it pins the committed recipe's ``recipe_sha``
into the ``approved_recipe_sha`` setting so subsequent scored reads stamp
``recipe_approved=true``. Until it is run, every payload is ``recipe_approved=false``
and a single deduped DeferredQuestion per new sha asks a human to approve or reject.

Non-zero exits use ``raise SystemExit(N)`` — this runs under ``call_command``.
"""

import hashlib
import json
import os
from typing import Annotated

import typer
from django_typer.management import TyperCommand, command

from teatree.config import get_effective_settings
from teatree.core.factory.factory_recipe import load_recipe
from teatree.core.factory.factory_score import FactoryScore, score
from teatree.core.models import ConfigSetting
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.factory_score_snapshot import FactoryScoreSnapshot
from teatree.utils.git_branch import head_sha


def _overlay() -> str:
    return os.environ.get("T3_OVERLAY_NAME", "")


def _recipe_approval_dedup_key(recipe_sha: str) -> str:
    """A namespaced 64-char dedup key so exactly one approval question exists per sha."""
    return hashlib.sha256(f"recipe_approval:{recipe_sha}".encode()).hexdigest()


def _render(result: FactoryScore) -> str:
    agg = "None (untrustworthy)" if result.aggregate is None else f"{result.aggregate:.4f}"
    coverage = f"{result.coverage:.2f}/{result.coverage_floor:.2f}"
    lines = [
        f"factory score: {agg}  verdict={result.verdict}  coverage={coverage}",
        f"recipe {result.recipe_sha[:12]} approved={result.recipe_approved} window={result.window_days}d",
    ]
    for sig in result.signals:
        norm = "—" if sig.normalized is None else f"{sig.normalized:.3f}"
        lines.append(
            f"  {sig.provider_id}: status={sig.status} value={sig.value} norm={norm} weight={sig.weight} red={sig.red}"
        )
    return "\n".join(lines)


def _queue_recipe_approval(recipe_sha: str, overlay: str) -> bool:
    """Queue one approval question per unapproved sha; ``True`` if a new row was written.

    Deduped on the sha-derived ``options_hash`` so a re-scored read against the
    same unapproved recipe never queues a second question.
    """
    dedup_key = _recipe_approval_dedup_key(recipe_sha)
    if DeferredQuestion.objects.filter(options_hash=dedup_key).exists():
        return False
    scope = overlay or "global"
    DeferredQuestion.record(
        f"The factory-score recipe (sha {recipe_sha[:12]}) is not yet approved for {scope}. "
        f"Review the evals/recipe.yaml diff, then run `t3 {overlay or '<overlay>'} recipe approve` to pin it.",
        options_json=json.dumps(["approve", "reject"]),
        options_hash=dedup_key,
    )
    return True


class Command(TyperCommand):
    @command()
    def score(
        self,
        *,
        window_days: Annotated[
            int,
            typer.Option("--window-days", help="Trailing window width in days (default 28)."),
        ] = 28,
        json_output: Annotated[
            bool,
            typer.Option("--json", help="Emit the score payload as JSON instead of the human view."),
        ] = False,
        record: Annotated[
            bool,
            typer.Option("--record", help="Persist a FactoryScoreSnapshot (refused unless factory_score_enabled)."),
        ] = False,
    ) -> str:
        """Compute the recipe-weighted factory score over the trailing window.

        Read-only by default (safe for calibration even when the flag is OFF).
        ``--record`` persists a snapshot and — for an unapproved recipe — queues a
        single human-approval question, but ONLY when ``factory_score_enabled`` is
        on; otherwise it refuses and writes nothing.
        """
        overlay = _overlay()
        settings = get_effective_settings(overlay or None)
        result = score(
            window_days=window_days,
            overlay=overlay,
            approved_recipe_sha=settings.approved_recipe_sha,
        )
        if record:
            if not settings.factory_score_enabled:
                self.stderr.write(
                    "  refusing --record: factory_score_enabled is off (the shipped OFF state). "
                    "The score computes read-only; recording is a deliberate later act."
                )
                raise SystemExit(2)
            FactoryScoreSnapshot.objects.record_snapshot(result, tree_sha=_safe_head_sha(), overlay=overlay)
            if not result.recipe_approved and _queue_recipe_approval(result.recipe_sha, overlay):
                self.stderr.write(f"  recipe {result.recipe_sha[:12]} unapproved — queued one approval question.")
        if json_output:
            return json.dumps(result.to_dict())
        return _render(result)

    @command()
    def approve(self) -> str:
        """Pin the committed recipe's sha into ``approved_recipe_sha`` (the human EVOLVE gate).

        Writes the current ``recipe.yaml`` sha to the ``ConfigSetting`` store in
        this overlay's scope (global when no overlay is active), so subsequent
        scored reads stamp ``recipe_approved=true``. Re-run after any recipe edit.
        """
        overlay = _overlay()
        recipe = load_recipe()
        ConfigSetting.objects.set_value("approved_recipe_sha", recipe.recipe_sha, scope=overlay)
        stored = ConfigSetting.objects.get_effective("approved_recipe_sha", scope=overlay)
        scope = overlay or "global"
        return f"  approved recipe {recipe.recipe_sha[:12]} for {scope} (stored={str(stored)[:12]})"


def _safe_head_sha() -> str:
    """The HEAD sha for provenance, or ``""`` when not inside a git tree."""
    try:
        return head_sha()
    except RuntimeError:
        return ""
