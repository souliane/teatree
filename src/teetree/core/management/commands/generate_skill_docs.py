from pathlib import Path

from django_typer.management import TyperCommand, command

from teetree.core.docgen import build_skill_doc_payload, render_skill_markdown, write_generated_doc


class Command(TyperCommand):
    help = "Generate deterministic TeaTree skill delegation documentation."

    @command()
    def handle(self, output_dir: str = "docs/generated", skill_map: str = "") -> str:
        out = Path(output_dir)
        skill_map_path = Path(skill_map) if skill_map else None
        payload = build_skill_doc_payload(skill_map_path)
        json_path = out / "skill-delegation-matrix.json"
        markdown_path = out / "skill-delegation-matrix.md"
        write_generated_doc(json_path, markdown_path, payload, render_skill_markdown(payload))
        self.stdout.write(str(markdown_path))
        return str(markdown_path)
