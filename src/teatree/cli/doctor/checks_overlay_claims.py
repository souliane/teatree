"""`t3 doctor` contested-repo advisory — one repo claimed by two overlays whose gates disagree.

``infer_overlay_for_url`` answers "whose settings govern an action on this repo?" from each
overlay's declared repos. Two overlays declaring the SAME repo is not itself an error — the
resolver is ambiguity-safe and either lets the more specific tier win or returns ``""``. What
IS an error is a contested repo whose claimants configure the guard rails DIFFERENTLY: the
verdict then depends on which tier happened to match, or on which overlay the caller's cwd
anchored to, rather than on the repo the action addresses.

The instance that motivated this: a fork repo hosting BOTH a product overlay's package and the
vendored core it is a fork of. Its root ``manage.py`` anchored one overlay and the vendored
``manage.py`` one directory down anchored the other, so the same ``t3 review approve`` proceeded
from the repo root and refused from ``vendor/``. Both answers were "correct" for the overlay that
answered; neither had been chosen. The claim was readable the whole time; its being CONTESTED,
and the settings that made the contest matter, were not.

**Advisory, never a failure.** A shared repo is a legitimate configuration and this check has no
way to know which claimant the operator meant. It changes no exit code and gates nothing. What it
removes is the indistinguishability: today a contested repo and an uncontested one look identical,
and after this the contest is NAMED together with the exact keys the claimants disagree on.
"""

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import typer

logger = logging.getLogger(__name__)

#: A repo is contested from the second claimant on — one overlay declaring it is the
#: ordinary case and can never disagree with itself.
CONTESTED_FROM_CLAIMANTS: int = 2

#: The resolved settings that decide whether a guarded action proceeds. Two overlays
#: claiming one repo only MATTERS when they answer one of these differently — an
#: unrelated divergence (a Slack channel, a token path) changes no verdict, and
#: reporting it would bury the findings that do. Declared here, beside the check that
#: reads them, so adding a gate needs no edit elsewhere.
CONTESTED_GATE_KEYS: tuple[str, ...] = (
    "on_behalf_post_mode",
    "require_human_approval_to_merge",
    "require_human_approval_to_answer",
    "autonomy",
    "mode",
)


@dataclass(frozen=True, slots=True)
class ContestedRepo:
    """One repo more than one overlay claims, with the gate keys they disagree on."""

    slug: str
    claimants: tuple[str, ...]
    disagreements: tuple[tuple[str, tuple[tuple[str, str], ...]], ...]
    """``(gate key, ((overlay, rendered value), …))`` for each key the claimants differ on."""


def contested_repos(
    *,
    claims: Iterable[tuple[str, Iterable[str]]],
    gates: Mapping[str, Mapping[str, object]],
) -> tuple[ContestedRepo, ...]:
    """The repos in *claims* held by 2+ overlays that ALSO disagree on a gate in *gates*.

    Pure, so the judgement is testable without a database or an overlay registry and the
    doctor wrapper is left with only the reads. *claims* is ``(overlay, declared repo
    slugs)``; *gates* maps an overlay to its resolved :data:`CONTESTED_GATE_KEYS` values.

    A repo is CONTESTED when two claimants declare it, and REPORTED only when at least one
    gate key differs between them. Agreeing claimants are silent by design: whichever tier
    wins produces the same verdict, so naming them would be noise the real finding hides in.

    A claimant absent from *gates* contributes no value for any key, which reads as a
    disagreement with every claimant that has one — an overlay whose settings could not be
    resolved is exactly the case a silent tie-break must not be trusted for.
    """
    declared: dict[str, list[str]] = {}
    for overlay, slugs in claims:
        for slug in slugs:
            if not (key := _claim_key(slug)):
                continue
            if overlay not in declared.setdefault(key, []):
                declared[key].append(overlay)

    findings: list[ContestedRepo] = []
    for slug, claimants in sorted(declared.items()):
        if len(claimants) < CONTESTED_FROM_CLAIMANTS:
            continue
        disagreements = tuple(
            (gate, tuple((name, _render(gates.get(name, {}).get(gate))) for name in claimants))
            for gate in CONTESTED_GATE_KEYS
            if len({_render(gates.get(name, {}).get(gate)) for name in claimants}) > 1
        )
        if disagreements:
            findings.append(ContestedRepo(slug=slug, claimants=tuple(claimants), disagreements=disagreements))
    return tuple(findings)


def _claim_key(slug: str) -> str:
    """The comparable identity of a declared repo: its trailing name segment, case-folded.

    Two overlays declare the same repo in different SHAPES — one enumerates the full
    ``owner/repo`` slug, the other only the bare directory name (both are legitimate, and
    ``infer_overlay_for_url`` matches each in its own tier). Comparing the raw strings would
    therefore miss every contest that matters, which is precisely the pair of shapes the
    two-tier resolver exists to arbitrate between.

    Reducing to the name segment DELIBERATELY over-approximates: two genuinely different
    repos sharing a basename across namespaces read as one claim. That is not a modelling
    error but a faithful one — ``_bare_name_owns`` matches on exactly that segment, so a
    basename collision is a real ambiguity in the resolver, and an advisory that hid it
    would be hiding the resolver's own behaviour.
    """
    return slug.strip().strip("/").rsplit("/", 1)[-1].lower()


def _render(value: object) -> str:
    """A gate value as the string the comparison and the report both use.

    One rendering for both, so a difference is never reported on a pair the comparison
    judged equal. ``None`` (an unresolved overlay or an unset key) renders as its own
    marker rather than as an empty string, which a genuine empty value could collide with.
    A ``StrEnum`` renders as its ``value`` so a stored row and a resolved enum of the same
    setting compare equal.
    """
    if value is None:
        return "<unresolved>"
    return str(getattr(value, "value", value))


def _finding_lines(finding: ContestedRepo) -> list[str]:
    """The advisory block for one contested repo — the claimants, then each disputed gate."""
    lines = [f"WARN  {finding.slug} is claimed by {' and '.join(finding.claimants)}, which disagree on:"]
    lines.extend(
        f"        {gate}: " + ", ".join(f"{name}={value}" for name, value in values)
        for gate, values in finding.disagreements
    )
    return lines


def _declared_claims() -> list[tuple[str, list[str]]]:
    """``(overlay, its declared workspace repos)`` for every registered overlay."""
    from teatree.core.overlay_loader import _overlay_repo_slugs_for_inference  # noqa: PLC0415 — deferred: needs apps

    return [(name, list(slugs)) for name, slugs in _overlay_repo_slugs_for_inference()]


def _resolved_gates(overlays: Iterable[str]) -> dict[str, dict[str, object]]:
    """The resolved :data:`CONTESTED_GATE_KEYS` for each overlay — the values, not the rows.

    Resolved per overlay rather than read off ``ConfigSetting``: the question is what a
    guarded action would actually DO under each claimant, and a raw row misses the shipped
    default, the global scope and the autonomy collapse that sit between the row and the
    verdict. An overlay whose settings will not resolve is simply left out of the mapping,
    which :func:`contested_repos` already reads as "disagrees with everyone".
    """
    from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: needs apps

    gates: dict[str, dict[str, object]] = {}
    for name in overlays:
        try:
            settings = get_effective_settings(overlay_name=name)
        except Exception:
            logger.warning("Overlay %r settings did not resolve while sweeping contested repos", name, exc_info=True)
            continue
        gates[name] = {gate: getattr(settings, gate, None) for gate in CONTESTED_GATE_KEYS}
    return gates


def _check_repos_claimed_by_disagreeing_overlays() -> None:
    """Name every repo two overlays claim while configuring its guard rails differently.

    Surfacing-only — it never gates the exit code, like the sibling advisories, because a
    shared repo can be deliberate. Crash-proof: any error degrades to one WARN line so a
    doctor run always completes and one broken probe cannot hide every other finding.
    """
    try:
        claims = _declared_claims()
        findings = contested_repos(claims=claims, gates=_resolved_gates(name for name, _ in claims))
    except Exception as exc:  # noqa: BLE001 — doctor check must never crash the run
        typer.echo(f"WARN  Contested-repo check crashed: {exc.__class__.__name__}: {exc}")
        return
    if not findings:
        return
    typer.echo(
        f"WARN  {len(findings)} repo(s) are claimed by more than one overlay with CONFLICTING gates. "
        "A guarded action on such a repo resolves to whichever claimant the caller happened to "
        "anchor on. Give the repo to one overlay: drop it from the other's declared repos."
    )
    for finding in findings:
        for line in _finding_lines(finding):
            typer.echo(line)
