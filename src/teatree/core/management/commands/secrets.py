"""Inspect and manage sealed secret rows; read secrets for pipes and child processes."""

import os
import re
import sys
from typing import Annotated, NoReturn

import typer
from django.utils import timezone
from django_typer.management import TyperCommand, command, initialize

from teatree.config.credential_pass_key import validate_pass_key_entry
from teatree.core.models.sealed_secret import SealedSecret
from teatree.core.models.secret_cutover import SecretCutover
from teatree.core.sealed.pass_import import PasswordStoreDirectoryError, apply_import, build_import_plan
from teatree.core.sealed.references import referenced_entry_names
from teatree.core.sealed.store import DbStore
from teatree.utils.master_key import master_key_source
from teatree.utils.run import run_streamed
from teatree.utils.secret_backups import backup_name
from teatree.utils.secrets import SELECTOR_ENV, PassStore, SecretNotFoundError, SecretStoreError, read_pass_required

_ENV_ASSIGNMENT = re.compile(r"^(?P<variable>[A-Za-z_][A-Za-z0-9_]*)=(?P<entry>.+)$")
EXIT_ABSENT = 1
EXIT_UNREADABLE = 2
EXIT_TERMINAL = 3


def _refuse(message: str, code: int) -> NoReturn:
    typer.echo(message, err=True)
    raise SystemExit(code)


def _refuse_unsafe_copy() -> None:
    if os.environ.get(SELECTOR_ENV) == "sealed":
        _refuse(f"refusing to copy from pass: {SELECTOR_ENV}=sealed, so the sealed rows are the truth", EXIT_ABSENT)
    try:
        source = master_key_source()
    except SecretStoreError as exc:
        _refuse(str(exc), EXIT_UNREADABLE)
    if source.in_volume:
        _refuse(
            f"refusing to copy from pass: the master key {source.label} sits inside the control-database volume "
            "(deploy/README.md)",
            EXIT_ABSENT,
        )


def _entry_name(name: str) -> str:
    try:
        return validate_pass_key_entry(name)
    except ValueError as exc:
        _refuse(str(exc), EXIT_ABSENT)


def _read_active(name: str) -> str:
    try:
        return read_pass_required(name)
    except SecretNotFoundError as exc:
        _refuse(str(exc), EXIT_ABSENT)
    except SecretStoreError as exc:
        _refuse(str(exc), EXIT_UNREADABLE)


class Command(TyperCommand):
    @initialize()
    def init(self) -> None:
        pass

    @command()
    def status(self) -> None:
        """Report key health without disclosing stored values."""
        source = master_key_source()
        health = SealedSecret.objects.key_health(source)
        typer.echo(f"store {os.environ.get(SELECTOR_ENV) or 'pass'}")
        typer.echo(f"source {source.label}")
        typer.echo(f"health {health.status}: {health.detail}")
        if health.row_count:
            for key_id, count in sorted(SealedSecret.objects.key_census().items()):
                typer.echo(f"key_id {key_id}: {count} rows")
        if not health.healthy:
            raise SystemExit(1)

    @command()
    def ls(self, prefix: Annotated[str, typer.Argument(help="Only names starting with this prefix.")] = "") -> None:
        """List sealed row names without decrypting values."""
        for name in SealedSecret.objects.names_starting(prefix):
            typer.echo(name)

    @command(name="import-pass")
    def import_pass(
        self,
        *,
        apply: Annotated[bool, typer.Option("--apply", help="Copy the row entries into the sealed store.")] = False,
    ) -> None:
        """Classify password-store names; with --apply copy them into sealed rows and print names and statuses only."""
        if apply:
            _refuse_unsafe_copy()
        try:
            plan = build_import_plan(check_readability=not apply)
        except PasswordStoreDirectoryError as exc:
            typer.echo(str(exc), err=True)
            raise SystemExit(1) from exc
        if not apply:
            for entry in plan:
                typer.echo(f"{entry.destination}\t{entry.readability}\t{entry.name}")
            return
        outcomes = apply_import(PassStore(), DbStore(), plan, now=timezone.now())
        for outcome in outcomes:
            typer.echo(f"{outcome.status}\t{outcome.name}")
        if any(outcome.status in {"failed", "empty"} for outcome in outcomes):
            raise SystemExit(1)
        SecretCutover.objects.get_or_create(pk=1)

    @command(name="set")
    def store_value(self, name: Annotated[str, typer.Argument(help="Entry name.")] = "") -> None:
        """Seal a value read from stdin (or a hidden prompt) under NAME in the sealed store."""
        entry = _entry_name(name)
        raw = typer.prompt("Value", hide_input=True) if sys.stdin.isatty() else sys.stdin.read()
        value = raw.removesuffix("\n")
        if not value.strip():
            _refuse(f"refusing to store an empty value under {entry!r}", EXIT_ABSENT)
        try:
            store = DbStore()
            with store.locked():
                existing = store.read(entry)
                if existing == value:
                    typer.echo(f"unchanged {entry} in the sealed secret store")
                    return
                if existing is not None:
                    store.write(backup_name(entry, timezone.now()), existing)
                store.write(entry, value)
                read_back = store.read(entry)
        except SecretStoreError as exc:
            _refuse(str(exc), EXIT_UNREADABLE)
        if read_back != value:
            _refuse(f"sealed secret {entry!r} did not read back after the write", EXIT_UNREADABLE)
        typer.echo(f"{'replaced' if existing is not None else 'stored'} {entry} in the sealed secret store")

    @command()
    def rm(self, name: Annotated[str, typer.Argument(help="Entry name.")] = "") -> None:
        """Remove NAME from the sealed store."""
        entry = _entry_name(name)
        try:
            removed = DbStore().remove(entry)
        except SecretStoreError as exc:
            _refuse(str(exc), EXIT_UNREADABLE)
        if not removed:
            _refuse(f"{entry} is not in the sealed secret store", EXIT_ABSENT)
        typer.echo(f"removed {entry} from the sealed secret store")

    @command()
    def verify(self) -> None:
        """Print ok|absent|unreadable per referenced entry, never a value; exit 1 if any is unreadable."""
        try:
            store = DbStore()
        except SecretStoreError as exc:
            _refuse(str(exc), EXIT_UNREADABLE)
        unreadable = False
        for name in sorted(referenced_entry_names()):
            try:
                status = "ok" if store.read(name) is not None else "absent"
            except SecretStoreError:
                status = "unreadable"
                unreadable = True
            typer.echo(f"{status}\t{name}")
        if unreadable:
            raise SystemExit(1)

    @command()
    def get(self, name: Annotated[str, typer.Argument(help="Entry name.")] = "") -> None:
        """Print the first line of NAME from the active store; exit 1 absent, 2 unreadable, 3 on a terminal."""
        entry = _entry_name(name)
        if sys.stdout.isatty():
            _refuse(
                f'refusing to print a secret to a terminal; capture it: VAR="$(t3 <overlay> secrets get {entry})"',
                EXIT_TERMINAL,
            )
        typer.echo(_read_active(entry))

    @command(name="exec")
    def run_with_secrets(
        self,
        command: Annotated[list[str] | None, typer.Argument(help="Command to run after --.")] = None,
        env: Annotated[list[str] | None, typer.Option("--env", help="VAR=<entry name>; repeatable.")] = None,
    ) -> None:
        """Run COMMAND with each --env VAR=<entry> value in the child's environment only."""
        if not command:
            _refuse("secrets exec needs a command after --", EXIT_ABSENT)
        environment = dict(os.environ)
        for assignment in env or []:
            match = _ENV_ASSIGNMENT.match(assignment)
            if match is None:
                _refuse(f"--env takes VAR=<entry name>, not {assignment!r}", EXIT_ABSENT)
            environment[match["variable"]] = _read_active(_entry_name(match["entry"]))
        raise SystemExit(run_streamed(command, env=environment, check=False))
