"""Render the shared settings comparison through Django's command boundary."""

import shutil
from pathlib import Path
from typing import IO, Annotated, cast

import typer

from teatree.core.machine_output import MachineOutputCommand, emit
from teatree.core.settings.settings_compare import build_compare_view
from teatree.core.settings.settings_compare_terminal import KIND_SLUGS, compare_payload, render_compare
from teatree.core.settings.settings_files import load_snapshots


class Command(MachineOutputCommand):
    help = "Render the same settings comparison as the dashboard, including peers and saved snapshots."

    def handle(
        self,
        snapshots: Annotated[list[str] | None, typer.Option("--snapshot")] = None,
        kinds: Annotated[list[str] | None, typer.Option("--kind")] = None,
        timeout: Annotated[float | None, typer.Option("--timeout")] = None,
        *,
        full: Annotated[bool, typer.Option("--full")] = False,
        json_output: Annotated[bool, typer.Option("--json")] = False,
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
        view = build_compare_view(loaded.snapshots, timeout=timeout, kinds=selected)
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
