"""Single source of which FSM models become generated Mermaid diagrams.

The generator (``generate_fsm_diagrams.py``) and the drift gate
(``check_fsm_diagrams_sync.py``) both loop over :func:`specs`, so a new model's
lifecycle diagram is wired by adding one :class:`DiagramSpec` here — markers,
canonical page path, and title all derive from its ``slug``.

Only models whose FSM is declared with django-fsm ``@transition`` decorators
belong here: ``render_fsm_mermaid`` introspects ``get_all_transitions``, so a
model that mutates its ``FSMField`` imperatively (e.g. ``Task``, whose
``claim``/``complete``/``fail``/``reopen`` take a row lock rather than a
``@transition``) would render an edge-less graph and is intentionally absent.

See: souliane/teatree#12
"""

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from django.db.models import Model

_DIAGRAMS_DIR = Path("docs/generated/diagrams")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def django_setup() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "teatree.settings")
    src = repo_root() / "src"
    if src.is_dir():
        sys.path.insert(0, str(src))
    import django

    django.setup()


@dataclass(frozen=True)
class DiagramSpec:
    slug: str
    model: type[Model]
    title: str
    consumers: tuple[Path, ...]
    field: str = "state"

    @property
    def begin(self) -> str:
        return f"<!-- BEGIN GENERATED: {self.slug}-fsm -->"

    @property
    def end(self) -> str:
        return f"<!-- END GENERATED: {self.slug}-fsm -->"

    @property
    def canonical(self) -> Path:
        return _DIAGRAMS_DIR / f"{self.slug}-lifecycle.md"

    def block(self) -> str:
        from teatree.core.diagrams import fenced_mermaid, render_fsm_mermaid

        return fenced_mermaid(render_fsm_mermaid(self.model, field=self.field))

    def canonical_document(self, block: str) -> str:
        return f"# {self.title}\n\n{self.begin}\n{block}\n{self.end}\n"


def specs() -> list[DiagramSpec]:
    from teatree.core.models import PullRequest, Ticket, Worktree

    readme = Path("README.md")
    return [
        DiagramSpec("worktree", Worktree, "Worktree lifecycle", (readme, Path("skills/workspace/SKILL.md"))),
        DiagramSpec("pull-request", PullRequest, "PullRequest lifecycle", (readme,)),
        DiagramSpec("ticket", Ticket, "Ticket lifecycle", (readme,)),
    ]
