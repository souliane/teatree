# Diagrams as Code

Store diagram sources here. Render during CI. Reference from docs, README, and skills.

## Conventions

- **Mermaid** for flowcharts and sequence diagrams (renders natively in GitHub markdown)
- **PlantUML** for complex class/component diagrams (requires CI rendering)
- Source files: `*.mmd` (Mermaid) or `*.puml` (PlantUML)
- Generated outputs: `*.svg` or `*.png` (gitignored, regenerated in CI)

## Usage

Reference diagrams from markdown using relative paths:

```markdown
![Architecture](docs/diagrams/architecture.svg)
```

## CI Integration

Add a workflow step that renders all diagram sources:

```yaml
- name: Render diagrams
  run: |
    npx @mermaid-js/mermaid-cli -i docs/diagrams/*.mmd -o docs/diagrams/
```
