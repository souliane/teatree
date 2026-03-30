from pathlib import Path

from django_typer.management import TyperCommand, command

from teatree.core.docgen import build_overlay_doc_payload, render_overlay_markdown, write_generated_doc


class Command(TyperCommand):
    help = "Generate deterministic overlay extension-point documentation."

    @command()
    def handle(self, output_dir: str = "docs/generated") -> str:
        out = Path(output_dir)
        payload = build_overlay_doc_payload()
        json_path = out / "overlay-extension-points.json"
        markdown_path = out / "overlay-extension-points.md"
        write_generated_doc(json_path, markdown_path, payload, render_overlay_markdown(payload))
        self.stdout.write(str(markdown_path))
        return str(markdown_path)
