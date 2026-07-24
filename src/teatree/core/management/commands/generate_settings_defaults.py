"""``generate_settings_defaults`` — regenerate ``src/teatree/config/defaults.toml``.

Read-only over the live ``ConfigSetting`` store: the generator adopts an
operator's live GLOBAL-scope tunables into the shipped defaults where that is
safe (see :mod:`teatree.config.defaults_generator` for the O2 policy) and keeps
the conservative in-code default everywhere else. SECRET / PERSONAL keys are
never emitted; overlay-scope rows and stale keys are reported, not shipped.

The baseline in-code defaults are read from the current ``defaults.toml``
``[teatree]`` table (the Phase-1 canonical extraction of the dataclass defaults),
so regenerating with no live overrides is a byte-stable no-op. ``--no-adopt-live``
ignores the DB entirely (the offline path when the live box is unreachable);
``--dry-run`` prints without writing.
"""

import tomllib
from pathlib import Path
from typing import Annotated

import typer
from django_typer.management import TyperCommand, command

from teatree.config.defaults_generator import GenerationReport, generate_defaults
from teatree.core.models import ConfigSetting
from teatree.hooks.banned_term_registry import export_scan_terms
from teatree.hooks.term_match import matched_term

_DEFAULTS_TOML = Path(__file__).resolve().parents[3] / "config" / "defaults.toml"
_GLOBAL_SCOPE = ""


class Command(TyperCommand):
    help = "Regenerate config/defaults.toml from in-code defaults + safe live-adopted tunables."

    @command()
    def handle(
        self,
        output: Annotated[str, typer.Option(help="Path to write the generated defaults.toml.")] = str(_DEFAULTS_TOML),
        dry_run: Annotated[  # noqa: FBT002 — typer convention; a bool flag with a default
            bool,
            typer.Option("--dry-run", help="Print the file + report without writing."),
        ] = False,
        no_adopt_live: Annotated[  # noqa: FBT002 — typer convention; a bool flag with a default
            bool,
            typer.Option("--no-adopt-live", help="Ignore the live DB; emit the in-code defaults only."),
        ] = False,
    ) -> None:
        baseline = tomllib.loads(_DEFAULTS_TOML.read_text(encoding="utf-8"))["teatree"]
        live_global = {} if no_adopt_live else ConfigSetting.objects.overrides_for_scope(_GLOBAL_SCOPE)
        overlay_scope_rows = (
            []
            if no_adopt_live
            else list(ConfigSetting.objects.exclude(scope=_GLOBAL_SCOPE).values_list("scope", "key"))
        )
        scan_terms = export_scan_terms()

        result = generate_defaults(
            in_code_defaults=baseline,
            live_global=live_global,
            overlay_scope_rows=overlay_scope_rows,
            banned_scan=lambda text: matched_term(text, scan_terms),
        )

        if dry_run:
            self.stdout.write(result.toml)
        else:
            Path(output).write_text(result.toml, encoding="utf-8")
        self._print_report(result.report, wrote=None if dry_run else output)

    def _print_report(self, report: GenerationReport, *, wrote: str | None) -> None:
        write = self.stderr.write
        write("=== generate_settings_defaults report ===")
        write(f"adopted-live ({len(report.adopted)}):")
        for a in sorted(report.adopted, key=lambda x: x.key):
            write(f"  {a.key}: {a.in_code_default!r} -> {a.emitted!r} (live)")
        write(f"kept-conservative — live override declined ({len(report.kept_conservative)}):")
        for a in sorted(report.kept_conservative, key=lambda x: x.key):
            reason = a.disposition.split(":", 1)[1]
            write(f"  {a.key}: kept {a.emitted!r} [{reason}]")
        if report.banned_aborted:
            write(f"banned-term ABORTED ({len(report.banned_aborted)}):")
            for a in report.banned_aborted:
                write(f"  {a.key}: {a.disposition}")
        write(f"skipped SECRET ({len(report.skipped_secret)}): {', '.join(report.skipped_secret) or '-'}")
        write(f"skipped PERSONAL ({len(report.skipped_personal)}): {', '.join(report.skipped_personal) or '-'}")
        write(f"stale/unknown keys ({len(report.stale_keys)}): {', '.join(report.stale_keys) or '-'}")
        write(f"overlay-scope rows reported ({len(report.overlay_scope_rows)}):")
        for scope, key in report.overlay_scope_rows:
            write(f"  [{scope}] {key}")
        write(f"wrote: {wrote}" if wrote else "dry-run: nothing written")
