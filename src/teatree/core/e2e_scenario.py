"""E2E seam value types: the authoring ``Scenario`` + the runner→seam context (#3329, #3331).

An overlay declares its per-feature acceptance scenarios as instances of the
frozen :class:`Scenario` dataclass and returns them from
:meth:`~teatree.core.overlay.OverlayE2E.scenarios`. Core owns the single
authoring shape here — one definition every overlay ships instances of, rather
than each overlay redeclaring the same field names beside its own ``Capture``
type and hand-mapping into the render dict.

:class:`E2eExtrasContext` is the frozen run context the runner hands
:meth:`~teatree.core.overlay.OverlayE2E.env_extras` so an overlay's extras key
off the *same* target core routed at (#3331). Both concerns live in this leaf
module so ``teatree.core.overlay`` (the seam) and the runner both import them
without a cycle.

These are pure value objects: no ORM, no code host, no CLI. The
authoring→render mapping (into the ``scenario-plan`` wire ``TypedDict``) lives
with the assembler in
:mod:`teatree.core.management.commands._test_plan.from_seams`.
"""

from dataclasses import dataclass, field

_UI_MODALITY = "ui"
_API_MODALITY = "api"


@dataclass(frozen=True, slots=True)
class E2eExtrasContext:
    """The resolved run context the runner hands :meth:`OverlayE2E.env_extras`.

    Every field is something core resolved for this run: ``target`` (the dual-env
    target ``"dev"`` / ``"qa"`` / ``"local"`` core routed at), ``spec_path`` (the
    selected Playwright spec), ``artifacts_dir`` (the out-of-repo capture root the
    runner exported as ``T3_E2E_ARTIFACTS_DIR``), and ``compose_project`` (the
    teatree-managed docker-compose project). An overlay reads these instead of
    re-deriving them, so its extras can never disagree with core's routing. A
    frozen context — not a widening parameter list — so a future field is an
    additive change, not another signature break.
    """

    target: str = ""
    spec_path: str = ""
    artifacts_dir: str = ""
    compose_project: str = ""


@dataclass(frozen=True, slots=True)
class Capture:
    """One evidence capture a scenario declares: a named ``slot`` and its caption.

    ``slot`` is the capture's stable identity — the assembler resolves it to a
    file under the run's artifacts dir (``<slot>`` or ``<slot>.png``), so a
    scenario names the captures it expects and core fails loud when a declared
    slot has no file. ``caption`` is the human line rendered above the image.
    """

    slot: str
    caption: str = ""


@dataclass(frozen=True, slots=True)
class Scenario:
    """One acceptance scenario an overlay authors for a spec.

    ``modality`` is ``"ui"`` (captioned screenshots) or ``"api"`` (a contract
    check with no screenshot). A ``"ui"`` scenario declares the captures it
    expects in ``captures``; an ``"api"`` scenario declares none. The render
    side (``scenario-plan`` template) receives the resolved image markdown for
    each capture — this authoring shape carries only the intent.
    """

    surface: str
    title: str = ""
    preconditions: str = ""
    steps: tuple[str, ...] = ()
    expected: str = ""
    modality: str = _UI_MODALITY
    captures: tuple[Capture, ...] = field(default_factory=tuple)

    @property
    def is_api(self) -> bool:
        """True when the scenario is an API-contract check (no screenshot)."""
        return self.modality == _API_MODALITY
