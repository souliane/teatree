"""Render the shared settings comparison through Django's command boundary."""

import shutil
from pathlib import Path
from typing import IO, Annotated, cast, override

import typer

from teatree.core.machine_output import MachineOutputCommand, emit
from teatree.core.settings.settings_compare import CompareFilters, build_compare_view
from teatree.core.settings.settings_compare_terminal import KIND_SLUGS, compare_payload, render_compare
from teatree.core.settings.settings_files import load_snapshots


class Command(MachineOutputCommand):
    help = (
        "What differs between this box and imported peers, and whether it can be read as drift. "
        "Exits non-zero only when fewer than two instances answered. A completed comparison "
        "succeeds even when instances are not comparable. COLUMNS sets table width for a redirected run."
    )

    @override
    def handle(
        self,
        snapshots: Annotated[
            list[str] | None,
            typer.Option(
                "--snapshot",
                help=(
                    "A saved snapshot JSON file to compare against, repeatable. "
                    "Import a capture from each peer to compare it with this instance."
                ),
            ),
        ] = None,
        kinds: Annotated[
            list[str] | None,
            typer.Option(
                "--kind",
                help="Show only these difference kinds, repeatable: values, override, code. Default shows all three.",
            ),
        ] = None,
        *,
        family: Annotated[str, typer.Option("--family", help="Show one setting or seed family.")] = "",
        group: Annotated[str, typer.Option("--group", help="Match a setting group by name.")] = "",
        text: Annotated[str, typer.Option("--text", help="Search row names and descriptions.")] = "",
        scope: Annotated[str, typer.Option("--scope", help="Global, all, or one overlay scope.")] = "global",
        all_rows: Annotated[bool, typer.Option("--all-rows", help="Include matching identical rows.")] = False,
        full: Annotated[
            bool, typer.Option("--full", help="Do not truncate a value, and print each row's import reason.")
        ] = False,
        json_output: Annotated[
            bool, typer.Option("--json", help="Emit the whole view as JSON, nothing summarised away.")
        ] = False,
    ) -> None:
        selected = []
        for slug in kinds or []:
            if slug not in KIND_SLUGS:
                self.stderr.write(f"unknown --kind {slug!r}; the kinds are: {', '.join(KIND_SLUGS)}")
                raise SystemExit(2)
            selected.append(KIND_SLUGS[slug])

        documents = []
        for path in snapshots or []:
            try:
                documents.append((path, Path(path).read_bytes()))
            except OSError as exc:
                self.stderr.write(f"{path}: {exc.strerror or exc}")
                raise SystemExit(2) from None
        loaded = load_snapshots(documents)
        filters = CompareFilters(scope=scope, family=family, group=group, text=text, differs_only=not all_rows)
        view = build_compare_view(loaded.snapshots, kinds=selected, filters=filters)
        human = render_compare(
            view,
            width=shutil.get_terminal_size(fallback=(100, 24)).columns,
            kinds=selected,
            full=full,
            refusals=loaded.refusals,
        )
        self.print_result = False
        emit(
            compare_payload(view, refusals=loaded.refusals),
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=human,
        )
        if view.error:
            raise SystemExit(1)
