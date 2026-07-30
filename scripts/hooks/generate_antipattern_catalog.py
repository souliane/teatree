"""Pre-commit hook: regenerate the anti-pattern catalog doc from the YAML.

Source of truth: ``src/teatree/quality/antipatterns.yaml``. This hook renders it
to ``docs/generated/antipattern-catalog.md`` (each entry anchored at ``#<id>``)
and auto-stages the file on change. Edit the YAML, never the doc.

See: souliane/teatree#166
"""

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from teatree.quality.catalog import AntiPatternEntry, load_catalog, load_catalog_text

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT = _REPO_ROOT / "docs" / "generated" / "antipattern-catalog.md"


def _severity_order(severity: str) -> int:
    return {"high": 0, "medium": 1, "low": 2}.get(severity, 3)


def _render_entry(e: AntiPatternEntry) -> list[str]:
    lines = [
        f"## {e.name}",
        "",
        f'<a id="{e.id}"></a>',
        "",
        f"- **id:** `{e.id}`",
        f"- **severity:** {e.severity}",
        f"- **detection:** {e.detection}",
    ]
    if e.grep_hint is not None:
        lines.append(f"- **grep hint:** `{e.grep_hint}`")
    lines.append(f"- **linter:** {f'`{e.linter}`' if e.linter else '_(none — gap)_'}")
    if e.eval_invariant:
        lines.append(f"- **eval invariant:** `{e.eval_invariant}`")
    lines.append(f"- **consumers:** {', '.join(e.consumers)}")
    if e.refs:
        lines.append(f"- **refs:** {', '.join(e.refs)}")
    lines += [
        "",
        f"**Anti-pattern.** {e.anti_pattern}",
        "",
        f"**Preferred.** {e.preferred_pattern}",
        "",
    ]
    if e.waivers:
        lines.append("**Accepted waivers.**")
        lines.append("")
        lines.extend(f"- {waiver}" for waiver in e.waivers)
        lines.append("")
    return lines


def build_markdown(catalog_text: str | None = None) -> str:
    entries = load_catalog_text(catalog_text) if catalog_text is not None else load_catalog()
    greppable = sum(1 for e in entries if e.detection == "greppable")
    judgement = len(entries) - greppable

    lines = [
        "# Architectural Anti-Pattern Catalog",
        "",
        "Generated from `src/teatree/quality/antipatterns.yaml` by",
        "`scripts/hooks/generate_antipattern_catalog.py`. Do not edit by hand —",
        "edit the YAML and regenerate.",
        "",
        "This is the single source of truth feeding the three review tiers:",
        "design-time (`architecture-design`), per-PR deterministic",
        "(`scripts/hooks/check_antipatterns.py`, manual stage), and periodic",
        "holistic (`ac-reviewing-codebase`).",
        "",
        f"**{len(entries)} entries** — {greppable} greppable, {judgement} judgement.",
        "",
        "## Index",
        "",
    ]
    lines.extend(
        f"- [{entry.name}](#{entry.id}) — {entry.severity}, {entry.detection}"
        for entry in sorted(entries, key=lambda e: (_severity_order(e.severity), e.id))
    )
    lines.append("")

    for entry in entries:
        lines += _render_entry(entry)

    return "\n".join(line.rstrip() for line in lines).rstrip("\n") + "\n"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    output = Path(args[0]) if args else _DEFAULT_OUTPUT

    old = output.read_text(encoding="utf-8") if output.is_file() else ""
    markdown = build_markdown()

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(markdown, encoding="utf-8")

    if markdown != old and output == _DEFAULT_OUTPUT and not os.environ.get("ANTIPATTERN_CATALOG_NO_STAGE"):
        subprocess.run(["git", "add", str(output)], check=False)
        print(f"Updated {output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
