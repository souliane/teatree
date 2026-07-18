from pathlib import Path

from django_typer.management import TyperCommand, command

from teatree.core.management_commands_doc import _APP_LABEL, write_management_commands_doc


class Command(TyperCommand):
    help = "Generate the management-commands reference doc from the live command tree."

    @command()
    def handle(self, output_dir: str = "docs/generated", app_label: list[str] | None = None) -> str:
        """Write the reference for the given app labels (default: ``teatree.core``).

        Pass ``--app-label`` (repeatable) to document a consuming project's own
        app alongside core, e.g. ``--app-label teatree.core --app-label my_app``.
        """
        labels = app_label or [_APP_LABEL]
        out = Path(output_dir)
        _, markdown_path = write_management_commands_doc(out, labels)
        self.stdout.write(str(markdown_path))
        return str(markdown_path)
