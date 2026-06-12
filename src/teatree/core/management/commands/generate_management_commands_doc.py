from pathlib import Path

from django_typer.management import TyperCommand, command

from teatree.core.management_commands_doc import write_management_commands_doc


class Command(TyperCommand):
    help = "Generate the management-commands reference doc from the live command tree."

    @command()
    def handle(self, output_dir: str = "docs/generated") -> str:
        out = Path(output_dir)
        _, markdown_path = write_management_commands_doc(out)
        self.stdout.write(str(markdown_path))
        return str(markdown_path)
