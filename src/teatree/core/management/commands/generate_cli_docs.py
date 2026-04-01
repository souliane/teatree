from pathlib import Path

from django_typer.management import TyperCommand, command

from teatree.core.docgen import build_cli_reference


class Command(TyperCommand):
    help = "Generate CLI reference documentation from --help introspection."

    @command()
    def handle(self, output: str = "docs/generated/cli-reference.md") -> str:
        out = Path(output)
        markdown = build_cli_reference()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(markdown, encoding="utf-8")
        self.stdout.write(str(out))
        return str(out)
