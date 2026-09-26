"""``snapshot_settings_defaults`` — report what this box's settings would propose.

``defaults.toml``'s ``[teatree]`` table is GENERATED from the settings declarations
(``config/declared_defaults.py``), so this command WRITES NOTHING: a value written here
is put back by the next render. What it still answers is the question the write used to
serve — which of this box's live global rows differ from what teatree ships, and what the
shipped file would look like if they were adopted.

Adopting one is therefore an edit to the DECLARATION that states it, reviewed as the code
change it is — and that review is the only approval there is, because a rendered file
cannot diverge from its in-code default for anything else to approve.

Safety-posture keys and dark feature-flags are declined by the planner and reported as
such, so a live override of one is visible here and adopted by no path.
"""

import difflib
from pathlib import Path

from django_typer.management import TyperCommand, command

from teatree.backends.slack.table_format import slack_table_fence
from teatree.config import cold_defaults
from teatree.config.defaults_snapshot import ShippedFile, SnapshotPlan, change_table, plan_snapshot
from teatree.core.models import ConfigSetting
from teatree.hooks.banned_term_registry import export_scan_terms
from teatree.hooks.term_match import matched_term

_GLOBAL_SCOPE = ""


class Command(TyperCommand):
    help = "Report which live global settings differ from the shipped defaults (writes nothing)."

    @command()
    def handle(self) -> None:
        plan = self._plan()
        self._print_report(plan)
        if not plan.changes:
            self.stdout.write("no change proposed — the shipped file already matches the live global settings.")
            return
        self.stdout.write(
            f"{len(plan.changes)} live row(s) differ from the shipped defaults — nothing written.\n"
            f"Adopt one by editing the declaration that states it, reviewed as a code change."
        )

    def _plan(self) -> SnapshotPlan:
        shipped = cold_defaults.shipped_defaults_table(cold_defaults.DEFAULTS_TOML)
        scan_terms = export_scan_terms()
        return plan_snapshot(
            shipped=ShippedFile(table=shipped, text=_current_text(cold_defaults.DEFAULTS_TOML)),
            live_global=ConfigSetting.objects.overrides_for_scope(_GLOBAL_SCOPE),
            overlay_scope_rows=list(ConfigSetting.objects.exclude(scope=_GLOBAL_SCOPE).values_list("scope", "key")),
            banned_scan=lambda text: matched_term(text, scan_terms),
        )

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
        if plan.changes:
            write("the file those rows would produce, against the one that ships:")
            write(_proposal_diff(_current_text(cold_defaults.DEFAULTS_TOML), plan.toml))


def _proposal_diff(shipped_text: str, proposed_text: str) -> str:
    """The proposal as a unified diff — the whole rendered file is unreadable in a terminal."""
    return "".join(
        difflib.unified_diff(
            shipped_text.splitlines(keepends=True), proposed_text.splitlines(keepends=True), "shipped", "proposed"
        )
    )


def _current_text(target: Path) -> str:
    """The shipped file as it stands — the base the re-rendered ``[teatree]`` slots back into."""
    try:
        return target.read_text(encoding="utf-8")
    except OSError:
        return ""
