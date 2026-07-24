"""Registered-consumer ↔ live-caller walk — the built-but-not-wired lane (souliane/teatree#3678).

The recurring integration failure this family fights is "the correct implementation
exists and the calling path does not use it": a component is BUILT and registered
but nothing invokes it on the path that needs it. ``test_gate_registry_walk.py``
covers the ``register_gate`` ↔ ``get_gate`` string seam; ``test_registry_parity.py``
covers ``AGENT_ZONES`` ↔ persistence executors; ``test_user_settings_readers.py``
covers dead config. Three registries where the two confirmed #3678 failures slipped
had no such net:

- **Doctor checks** — a ``_check_*`` probe defined in ``cli/doctor`` but wired into no
orchestration list is dead authority: it runs never, so its FAIL never fires.
- **Scanners** — a ``*Scanner`` class defined in ``loop/scanners`` but instantiated into
no job is shipped dark (the scenario-reachability check's exact shape).
- **Governor consumers** — a function that consults the adaptive admission governor
(``decide_admission`` / ``per_agent_test_workers`` in ``teatree.core.admission_governor``)
but that nothing calls is a governor lane wired to nothing — the headless-congestion
collapse was on a lane the governor did not gate.

Every lane is an introspective AST walk of ``src/teatree`` (never a hand-list, so it
cannot drift behind the code): a NEW registered member with no live caller fails the
PR that introduces it. The fourth lane pins the specific #3678 case — the HEADLESS
admission path references the governor, not only the interactive one.

Static by design: this proves "a caller exists in the code". The dynamic "the seam
actually fired in the last 24h" proof is the runtime reconciliation ledger, tracked
separately (#3678 § "Runtime counterpart").
"""

import ast
from collections.abc import Callable, Iterator
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parents[2] / "src" / "teatree"
_DOCTOR_DIR = _SRC_DIR / "cli" / "doctor"
_SCANNERS_DIR = _SRC_DIR / "loop" / "scanners"

#: The base scanner ``Protocol`` — a structural contract, never instantiated, so it is
#: not a "registered scanner" the wiring walk should demand a job for.
_SCANNER_PROTOCOL = "Scanner"

#: The governor's decision/cap API. A function referencing one of these IS a governor
#: consumer, derived from the call graph rather than hand-listed.
_GOVERNOR_DECISION_API = frozenset({"decide_admission", "per_agent_test_workers"})
#: The governor definition module itself — excluded from the consumer derivation.
_GOVERNOR_MODULE = "core/admission_governor.py"

# Scanners registered/exported but DELIBERATELY instantiated into no live job — the
# reviewable allowlist idiom the family shares (``ROUTES_WITHOUT_STATIC_PRODUCER`` /
# ``FIELDS_WITHOUT_SRC_READER``). ``CodexReviewScanner`` dispatches ``/codex:review``,
# but codex is not available on this host — the ``codex_reviewing`` lane is stale and
# the real re-wiring is tracked by souliane/teatree#3569. It stays exported (the class,
# markers, and CLI surface are kept) yet is wired into no job. A NEW never-instantiated
# scanner is NOT on this list and fails until it is wired or consciously allowlisted.
SCANNERS_WITHOUT_LIVE_INSTANTIATION: frozenset[str] = frozenset({"CodexReviewScanner"})

_DefOwner = tuple[str, str | None]  # (relative-file, containing-top-level-def-name | None)


def _src_trees() -> Iterator[tuple[str, ast.Module]]:
    """Every ``src/teatree`` module as (repo-relative path, parsed AST)."""
    for path in _SRC_DIR.rglob("*.py"):
        yield str(path.relative_to(_SRC_DIR)), ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _call_name(call: ast.Call) -> str | None:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)


def _referenced_name(node: ast.AST) -> str | None:
    """The identifier a Name-load or Attribute-access node references, else ``None``.

    Deliberately NOT an import alias or a string literal: a bare ``from x import
    _check_y`` re-export or a ``"_check_y"`` in ``__all__`` is not *wiring* — only a
    real reference (a call, or a bare name in a dispatch tuple) counts as a caller.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _references_by_owner() -> dict[str, set[_DefOwner]]:
    """``name -> {(file, containing-top-level-def)}`` for every reference under ``src``.

    The owner tag is the top-level def/class the reference sits inside (``None`` at
    module level), so a member referenced ONLY inside its own definition (a recursive
    self-call) is distinguishable from one a *different* site references.
    """
    references: dict[str, set[_DefOwner]] = {}
    for rel, tree in _src_trees():
        for top in tree.body:
            owner = top.name if isinstance(top, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) else None
            for sub in ast.walk(top):
                name = _referenced_name(sub)
                if name is not None:
                    references.setdefault(name, set()).add((rel, owner))
    return references


def _has_external_reference(name: str, def_file: str, references: dict[str, set[_DefOwner]]) -> bool:
    """True when *name* is referenced anywhere except inside its OWN definition.

    A reference at module level in the defining file (a dispatch tuple), inside another
    def, or in another file all count as a live caller; only the self-referential
    ``(def_file, name)`` owner — a recursive call inside the member's own body — does not.
    """
    return any(not (file == def_file and owner == name) for file, owner in references.get(name, ()))


def _named_defs(directory: Path, *, is_member: Callable[[ast.stmt], bool]) -> dict[str, str]:
    """Top-level ``FunctionDef``/``ClassDef`` matching *is_member* -> its repo-relative file."""
    members: dict[str, str] = {}
    for path in directory.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if is_member(node) and isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                members.setdefault(node.name, str(path.relative_to(_SRC_DIR)))
    return members


def doctor_checks() -> dict[str, str]:
    """Every ``_check_*`` / ``check_*`` probe defined in ``cli/doctor`` -> its file."""
    return _named_defs(
        _DOCTOR_DIR,
        is_member=lambda n: isinstance(n, ast.FunctionDef) and (n.name.startswith(("_check_", "check_"))),
    )


def scanner_classes() -> dict[str, str]:
    """Every concrete ``*Scanner`` class defined in ``loop/scanners`` (base Protocol excluded)."""
    return _named_defs(
        _SCANNERS_DIR,
        is_member=lambda n: isinstance(n, ast.ClassDef) and n.name.endswith("Scanner") and n.name != _SCANNER_PROTOCOL,
    )


def instantiated_names() -> set[str]:
    """Every callee name invoked as ``Name(...)`` under ``src`` — a class here is instantiated."""
    names: set[str] = set()
    for _rel, tree in _src_trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = _call_name(node)
                if name is not None:
                    names.add(name)
    return names


def governor_consumers() -> dict[str, str]:
    """Every top-level function consulting the governor decision/cap API -> its file.

    Derived from the call graph — a function whose body references ``decide_admission``
    or ``per_agent_test_workers`` — so a NEW governor consumer enrols automatically and
    must earn a caller. The governor's own definition module is excluded.
    """
    consumers: dict[str, str] = {}
    for rel, tree in _src_trees():
        if rel == _GOVERNOR_MODULE:
            continue
        for top in tree.body:
            if not isinstance(top, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            referenced = {sub.id for sub in ast.walk(top) if isinstance(sub, ast.Name)}
            if referenced & _GOVERNOR_DECISION_API:
                consumers[top.name] = rel
    return consumers


def module_calls(rel_file: str, name: str) -> bool:
    """True when the module at *rel_file* calls ``name(...)`` at least once."""
    tree = ast.parse((_SRC_DIR / rel_file).read_text(encoding="utf-8"))
    return any(isinstance(node, ast.Call) and _call_name(node) == name for node in ast.walk(tree))


def module_references(rel_file: str, targets: frozenset[str]) -> bool:
    """True when the module at *rel_file* references any identifier in *targets*."""
    tree = ast.parse((_SRC_DIR / rel_file).read_text(encoding="utf-8"))
    return any(_referenced_name(node) in targets for node in ast.walk(tree))


def called_from_another_module(name: str, own_file: str) -> str | None:
    """The first module OTHER than *own_file* that calls ``name(...)``, else ``None``."""
    for rel, tree in _src_trees():
        if rel == own_file:
            continue
        if any(isinstance(node, ast.Call) and _call_name(node) == name for node in ast.walk(tree)):
            return rel
    return None


# ── Pure predicates (shared by the real assertions and the anti-vacuity self-tests) ──


def unwired_doctor_checks(checks: dict[str, str], references: dict[str, set[_DefOwner]]) -> list[str]:
    """Doctor checks with no reference outside their own def — the dead-check class."""
    return sorted(name for name, file in checks.items() if not _has_external_reference(name, file, references))


def uninstantiated_scanners(scanners: dict[str, str], instantiated: set[str], allow: frozenset[str]) -> list[str]:
    """Scanner classes instantiated by no production caller and not allowlisted — shipped dark."""
    return sorted(name for name in scanners if name not in instantiated and name not in allow)


class TestEveryDoctorCheckHasALiveCaller:
    """A ``_check_*`` probe wired into no orchestration list is dead authority."""

    def test_no_doctor_check_is_defined_but_uncalled(self) -> None:
        unwired = unwired_doctor_checks(doctor_checks(), _references_by_owner())
        assert not unwired, (
            "doctor check(s) defined in cli/doctor but referenced by no production caller "
            "(dead authority — the check never runs, so its FAIL never fires): " + str(unwired)
        )


class TestEveryScannerIsInstantiated:
    """A ``*Scanner`` class instantiated into no job is shipped dark."""

    def test_no_scanner_is_defined_but_never_instantiated(self) -> None:
        uninstantiated = uninstantiated_scanners(
            scanner_classes(), instantiated_names(), SCANNERS_WITHOUT_LIVE_INSTANTIATION
        )
        assert not uninstantiated, (
            "scanner class(es) defined in loop/scanners but instantiated by no production job "
            "(built-but-not-wired) — wire it into a job factory or allowlist it on purpose: " + str(uninstantiated)
        )

    def test_allowlisted_scanners_are_still_real_and_still_uninstantiated(self) -> None:
        # A stale allowlist entry (a scanner that was removed, or one that is NOW wired)
        # is dead surface — the allowlist must not outlive its justification.
        scanners = scanner_classes()
        instantiated = instantiated_names()
        stale = sorted(n for n in SCANNERS_WITHOUT_LIVE_INSTANTIATION if n not in scanners)
        assert not stale, f"SCANNERS_WITHOUT_LIVE_INSTANTIATION entries that are not scanner classes: {stale}"
        now_wired = sorted(n for n in SCANNERS_WITHOUT_LIVE_INSTANTIATION if n in instantiated)
        assert not now_wired, f"allowlisted scanners that now HAVE a live instantiation (drop them): {now_wired}"


class TestEveryGovernorConsumerHasALiveCaller:
    """A function consulting the admission governor that nothing calls is a lane wired to nothing."""

    def test_no_governor_consumer_is_defined_but_uncalled(self) -> None:
        consumers = governor_consumers()
        orphaned = sorted(name for name, file in consumers.items() if called_from_another_module(name, file) is None)
        assert not orphaned, (
            "governor consumer(s) that consult decide_admission / per_agent_test_workers but that "
            "no other module calls (a governor lane wired to nothing): " + str(orphaned)
        )

    def test_the_known_consumers_are_all_discovered(self) -> None:
        # The derivation must actually find the interactive verdict, the headless deny
        # reason, AND the headless test-worker cap — the three live consumer seams.
        found = set(governor_consumers())
        assert {"governor_verdict", "headless_admission_denied_reason", "with_test_worker_cap"} <= found, sorted(found)


class TestHeadlessLaneWiresGovernor:
    """The #3678 case: the HEADLESS admission path references the governor, not only the interactive one."""

    _HEADLESS_ADMISSION_MODULE = "core/headless_admission.py"
    _HEADLESS_ENV_MODULE = "agents/_headless_env.py"
    _INTERACTIVE_CONSUMER = "governor_verdict"
    _HEADLESS_CONSUMER = "headless_admission_denied_reason"
    #: The three headless chokepoints that must gate on the governor: the post_save
    #: auto-enqueue, the drain safety net, and issue intake (#3644 / F9).
    _HEADLESS_CHOKEPOINTS = ("core/signals.py", "core/tasks.py", "loop/scanners/issue_intake.py")

    def test_headless_admission_module_consults_the_pure_governor_decision(self) -> None:
        assert module_references(self._HEADLESS_ADMISSION_MODULE, frozenset({"decide_admission"})), (
            "the headless admission module no longer references decide_admission — "
            "the headless lane has been un-wired from the governor"
        )

    def test_headless_env_cap_references_the_governor(self) -> None:
        targets = frozenset(_GOVERNOR_DECISION_API | {"admission_governor"})
        assert module_references(self._HEADLESS_ENV_MODULE, targets), (
            "the headless env test-worker cap no longer references the admission governor"
        )

    def test_every_headless_chokepoint_gates_on_the_governor(self) -> None:
        ungated = sorted(rel for rel in self._HEADLESS_CHOKEPOINTS if not module_calls(rel, self._HEADLESS_CONSUMER))
        assert not ungated, (
            "headless chokepoint(s) that no longer call headless_admission_denied_reason "
            "(the governor stops gating that lane): " + str(ungated)
        )

    def test_the_interactive_lane_also_still_gates_on_the_governor(self) -> None:
        # The contract is "BOTH lanes", so the interactive verdict must stay wired too —
        # this lane exists precisely because the governor was once interactive-only.
        assert called_from_another_module(self._INTERACTIVE_CONSUMER, "loop/admission.py") is not None


class TestConsumerCallerWalkCardinalityFloors:
    """Anti-vacuity — a broken enumerator that discovers nothing must not pass green."""

    def test_doctor_check_floor(self) -> None:
        assert len(doctor_checks()) >= 55, sorted(doctor_checks())

    def test_scanner_floor(self) -> None:
        assert len(scanner_classes()) >= 40, sorted(scanner_classes())

    def test_governor_consumer_floor(self) -> None:
        assert len(governor_consumers()) >= 3, sorted(governor_consumers())

    def test_reference_index_floor(self) -> None:
        # The whole-tree reference index must be densely populated, else the walk broke.
        assert len(_references_by_owner()) >= 500
        assert len(instantiated_names()) >= 200


class TestConsumerCallerWalkFiresRed:
    """Anti-vacuity — each lane must actually NAME an unwired member, not silently pass."""

    def test_an_unwired_doctor_check_is_named(self) -> None:
        # Run the REAL predicate over an injected check with no reference: it is named,
        # while a genuinely-referenced check is not.
        real = next(iter(doctor_checks()))
        checks = {real: doctor_checks()[real], "_check_synthetic_never_wired": "cli/doctor/checks_synthetic.py"}
        result = unwired_doctor_checks(checks, _references_by_owner())
        assert result == ["_check_synthetic_never_wired"], result

    def test_a_never_instantiated_scanner_is_named(self) -> None:
        result = uninstantiated_scanners(
            {"SyntheticNeverBuiltScanner": "loop/scanners/synthetic.py", "MyPrsScanner": "loop/scanners/my_prs.py"},
            instantiated_names(),
            frozenset(),
        )
        assert result == ["SyntheticNeverBuiltScanner"], result

    def test_an_allowlisted_scanner_is_not_named(self) -> None:
        # The allowlist genuinely suppresses: the same synthetic scanner, allowlisted, passes.
        result = uninstantiated_scanners(
            {"SyntheticNeverBuiltScanner": "loop/scanners/synthetic.py"},
            instantiated_names(),
            frozenset({"SyntheticNeverBuiltScanner"}),
        )
        assert result == [], result

    def test_a_governor_consumer_with_no_caller_is_detected(self) -> None:
        # The real caller-resolver finds nothing for a name nothing calls, so an injected
        # consumer of that name would be flagged — the orphaned-lane class fires.
        assert called_from_another_module("__synthetic_governor_consumer_no_caller__", "core/x.py") is None
        assert called_from_another_module("governor_verdict", "loop/admission.py") is not None

    def test_the_headless_wiring_probe_can_tell_wired_from_unwired(self) -> None:
        # The probe distinguishes a present reference from an absent one — without this
        # control a green headless-wiring assertion could be a broken probe.
        module = TestHeadlessLaneWiresGovernor._HEADLESS_ADMISSION_MODULE
        assert module_references(module, frozenset({"decide_admission"}))
        assert not module_references(module, frozenset({"__no_such_symbol_anywhere__"}))
