from pathlib import Path

from django_typer.management import TyperCommand, command

from teatree.core.evidence.doc_render import (
    build_skill_catalogue_payload,
    render_skill_catalogue_markdown,
    write_generated_doc,
)


class Command(TyperCommand):
    help = "Generate the skills catalogue from skills/*/SKILL.md frontmatter."

    @command()
    def handle(self, output_dir: str = "docs/generated", skills_root: str = "skills") -> str:
        out = Path(output_dir)
        payload = build_skill_catalogue_payload(Path(skills_root))
        json_path = out / "skills-catalogue.json"
        markdown_path = out / "skills-catalogue.md"
        write_generated_doc(json_path, markdown_path, payload, render_skill_catalogue_markdown(payload))
        self.stdout.write(str(markdown_path))
        return str(markdown_path)
