"""``snapshot_settings_defaults`` — propose the live box's settings onto ``defaults.toml``.

The shipped-defaults file is hand-editable and the resolver reads it, so writing it is a
change to what every fresh install gets. This command therefore NEVER writes unattended:

1. a bare run renders the proposed diff (key, shipped now, proposed, scope) as a
    monospace table and records ONE :class:`DeferredQuestion` carrying it, fingerprinted
    to that exact diff. Nothing is written.
2. the owner answers it through the existing seam — ``t3 teatree questions list`` then
    ``t3 teatree questions answer <id> approve``. That is the same human-approval
    primitive the directive loop's ratify step uses; there is no second mechanism here.
3. ``--apply`` re-derives the plan, requires an ``approve`` answer on a question whose
    fingerprint matches the CURRENT diff, and only then writes ``defaults.toml`` and
    reconciles ``defaults_approvals.toml`` (the divergence gate's review record).

The command posts no Slack message itself: the question body IS the rendered table, and
the existing ``DeferredQuestionPosterScanner`` mirrors un-mirrored questions to the
owner's DM on the next tick.

Safety-posture keys and dark feature-flags are declined by the planner, so a live
override of one is reported and never written — approval or not.
"""

import json
from pathlib import Path
from typing import Annotated

import typer
from django.utils import timezone
from django_typer.management import TyperCommand, command

from teatree.backends.slack.table_format import slack_table_fence
from teatree.config import cold_defaults
from teatree.config.defaults_approvals import (
    APPROVALS_TOML,
    ApprovedDivergence,
    read_approvals,
    render_approvals,
    shipped_divergences,
)
from teatree.config.defaults_snapshot import (
    ShippedFile,
    SnapshotPlan,
    change_table,
    pinned_fail_closed_keys,
    plan_fingerprint,
    plan_snapshot,
)
from teatree.core.models import ConfigSetting
from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionAudit
from teatree.hooks.banned_term_registry import export_scan_terms
from teatree.hooks.term_match import matched_term

_GLOBAL_SCOPE = ""
_QUESTION_PREFIX = "defaults_snapshot"
_APPROVAL_ANSWER = "approve"
_OPTIONS = json.dumps([{"label": "approve"}, {"label": "reject"}])


class Command(TyperCommand):
    help = "Propose a snapshot of the live global settings onto config/defaults.toml (owner-approved)."

    @command()
    def handle(
        self,
        apply: Annotated[  # noqa: FBT002 — typer convention; a bool flag with a default
            bool,
            typer.Option("--apply", help="Write the proposal the owner already approved."),
        ] = False,
    ) -> None:
        plan = self._plan()
        self._print_report(plan)
        if not plan.changes:
            self.stdout.write("no change proposed — the shipped file already matches the live global settings.")
            return
        if apply:
            self._apply(plan)
        else:
            self._propose(plan)

    def _plan(self) -> SnapshotPlan:
        shipped = cold_defaults.shipped_defaults_table(cold_defaults.DEFAULTS_TOML)
        scan_terms = export_scan_terms()
        return plan_snapshot(
            shipped=ShippedFile(table=shipped, text=_current_text(cold_defaults.DEFAULTS_TOML)),
            live_global=ConfigSetting.objects.overrides_for_scope(_GLOBAL_SCOPE),
            overlay_scope_rows=list(ConfigSetting.objects.exclude(scope=_GLOBAL_SCOPE).values_list("scope", "key")),
            banned_scan=lambda text: matched_term(text, scan_terms),
        )

    def _propose(self, plan: SnapshotPlan) -> None:
        """Record the owner's approval question carrying the rendered diff; write nothing."""
        marker = f"{_QUESTION_PREFIX}:{plan_fingerprint(plan.changes)}"
        question = DeferredQuestion.record(
            _question_body(plan, marker),
            options_json=_OPTIONS,
            options_hash=marker,
            dedupe_marker=marker,
        )
        self.stdout.write(
            f"proposed {len(plan.changes)} change(s) — nothing written.\n"
            f"approve with: t3 teatree questions answer {question.pk} approve\n"
            f"then apply with: manage.py snapshot_settings_defaults --apply"
        )

    def _apply(self, plan: SnapshotPlan) -> None:
        """Write the file + reconcile the ledger, only for an approved matching fingerprint."""
        marker = f"{_QUESTION_PREFIX}:{plan_fingerprint(plan.changes)}"
        question = (
            DeferredQuestion.objects.filter(options_hash=marker, answered_at__isnull=False)
            .order_by("-answered_at")
            .first()
        )
        if question is None:
            self.stderr.write(
                "refused: no answered approval for THIS diff. Run without --apply to propose it, "
                "then answer that question with `approve`."
            )
            raise SystemExit(1)
        if question.answer_text.strip().lower() != _APPROVAL_ANSWER:
            self.stderr.write(f"refused: question #{question.pk} was answered {question.answer_text.strip()!r}.")
            raise SystemExit(1)

        target = cold_defaults.DEFAULTS_TOML
        target.write_text(plan.toml, encoding="utf-8")
        recorded = self._reconcile_ledger(question)
        self.stdout.write(f"wrote {target} ({len(plan.changes)} change(s), {recorded} approval(s) recorded).")

    def _reconcile_ledger(self, question: DeferredQuestion) -> int:
        """Rewrite the ledger so it holds exactly the file's current approvable divergences.

        Derived from the file that was just written, never from the change list: a change
        that lands a value back ON its in-code default removes a divergence, so its
        approval entry must go with it rather than linger as a pre-authorization.
        """
        existing = read_approvals(APPROVALS_TOML)
        pinned = pinned_fail_closed_keys()
        approver = _approver_of(question)
        now = timezone.now().isoformat()
        entries: dict[str, ApprovedDivergence] = {}
        for key, divergence in shipped_divergences().items():
            if key in pinned:
                continue
            unchanged = existing.get(key)
            entries[key] = (
                unchanged
                if unchanged is not None and unchanged.value == divergence.shipped
                else ApprovedDivergence(
                    key=key,
                    value=divergence.shipped,
                    approver=approver,
                    question_id=question.pk,
                    recorded_at=now,
                )
            )
        APPROVALS_TOML.write_text(render_approvals(entries), encoding="utf-8")
        return len(entries)

    def _print_report(self, plan: SnapshotPlan) -> None:
        write = self.stderr.write
        write("=== snapshot_settings_defaults ===")
        headers, rows = change_table(plan.changes)
        write(slack_table_fence(headers, rows) if plan.changes else "proposed changes: none")
        write(f"declined — never movable through this path ({len(plan.declined)}):")
        for declined in sorted(plan.declined, key=lambda d: d.key):
            write(f"  {declined.key}: {declined.reason}")
        write(f"skipped SECRET ({len(plan.skipped_secret)}): {', '.join(plan.skipped_secret) or '-'}")
        write(f"skipped PERSONAL ({len(plan.skipped_personal)}): {', '.join(plan.skipped_personal) or '-'}")
        write(f"stale/unknown keys ({len(plan.stale_keys)}): {', '.join(plan.stale_keys) or '-'}")
        write(f"overlay-scope rows reported ({len(plan.overlay_scope_rows)}):")
        for scope, key in plan.overlay_scope_rows:
            write(f"  [{scope}] {key}")


def _current_text(target: Path) -> str:
    """The shipped file as it stands — the base the re-rendered ``[teatree]`` slots back into."""
    try:
        return target.read_text(encoding="utf-8")
    except OSError:
        return ""


def _approver_of(question: DeferredQuestion) -> str:
    """Who answered the question, from its audit row — blank when none recorded it."""
    audit = DeferredQuestionAudit.objects.filter(question=question, action="answered").order_by("-resolved_at").first()
    return audit.resolver_id if audit is not None else ""


def _question_body(plan: SnapshotPlan, marker: str) -> str:
    """The approval question — the diff as a monospace fence, never a pipe table."""
    headers, rows = change_table(plan.changes)
    declined = ", ".join(f"{d.key} ({d.reason})" for d in sorted(plan.declined, key=lambda d: d.key)) or "none"
    return (
        f"Approve a shipped-defaults snapshot? {len(plan.changes)} change(s) to config/defaults.toml.\n"
        f"These become the default for every fresh install.\n\n"
        f"{slack_table_fence(headers, rows)}\n"
        f"Declined (safety-posture / dark-flag / workflow keys never move here): {declined}\n"
        f"Answer `approve` to authorize exactly this diff ({marker}); anything else refuses it."
    )
