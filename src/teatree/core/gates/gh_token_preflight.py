"""GitHub token permission preflight (#3405).

The deploy loop drives GitHub through ``gh`` with ``TEATREE_GH_TOKEN``. A token
that authenticates (``gh auth status`` green) but lacks a *write* permission the
loop needs — ``issues: write`` for labelling/closing, ``pull_requests: write``
for opening/merging, ``contents: write`` for pushing — does not fail at deploy
time. It fails much later, mid-run, with ``Resource not accessible by personal
access token`` on the first ``gh issue edit`` / ``gh pr merge`` — a silent,
hard-to-diagnose block on autonomy.

This probes the token's *effective* permissions up front so the failure is a
one-line bootstrap error instead of a late runtime surprise. The probe is
side-effect-free: each write permission is checked with a mutation aimed at a
resource number that never exists (issue/PR ``0``, a bogus ref), so a token that
*has* the permission gets a harmless ``404`` while a token that *lacks* it gets
the route-level ``403 Resource not accessible`` GitHub returns before it ever
loads the resource. Nothing is created, edited, or deleted either way.

``deploy/entrypoint.sh`` runs the same contract in pure bash during ``init``
(before the editable install exists, so it cannot call ``t3``); the
``teatree.cli.doctor`` mirror check and this module share the canonical
:data:`REQUIRED_PERMISSION_LABELS`, and a test pins the entrypoint's labels to
it so the two implementations cannot drift.
"""

import shutil
from collections.abc import Callable
from dataclasses import dataclass

from teatree.utils.run import run_allowed_to_fail

# The permissions the deploy loop actually exercises, in report order. The
# labels are the human ``"<permission>: <level>"`` form GitHub's fine-grained
# token UI uses, so an operator can map a FAIL straight onto a token setting.
# Pinned to ``deploy/entrypoint.sh`` by ``tests/test_deploy_entrypoint_token_preflight``.
REQUIRED_PERMISSION_LABELS: tuple[str, ...] = (
    "metadata: read",
    "issues: write",
    "pull_requests: write",
    "contents: write",
)

# Substrings (lowercased) in a ``gh api`` failure that mean the *token* is
# denied — a permission/visibility signal, not a transient network fault. Used
# to tell "the token lacks this" apart from "the API was unreachable".
_DENIED_SIGNALS: tuple[str, ...] = (
    "not accessible",  # "Resource not accessible by personal access token / integration"
    "not found",  # a fine-grained token with no access sees the repo as 404
    "bad credentials",
    "requires authentication",
    "must be authenticated",
)

# The single write-permission signal: GitHub returns exactly this at the route
# level for a token missing the permission, before validating the target.
_FORBIDDEN_SIGNAL = "not accessible"

# (permission label, gh-api argv template) for the three write probes. Each
# mutates a resource id that cannot exist, so a permitted token gets a 404 and a
# denied token gets a 403 — never a real write.
_WRITE_PROBES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("issues: write", ("--method", "PATCH", "repos/{slug}/issues/0", "-f", "state=open")),
    ("pull_requests: write", ("--method", "PATCH", "repos/{slug}/pulls/0", "-f", "state=open")),
    ("contents: write", ("--method", "PATCH", "repos/{slug}/git/refs/heads/teatree-preflight-nonexistent")),
)

type GhRunner = Callable[[list[str]], tuple[int, str]]


@dataclass(frozen=True)
class GhTokenProbe:
    """Outcome of a token-permission probe.

    ``missing`` is the denied permission labels (empty == the token has every
    required permission). ``indeterminate_reason`` is set only when the probe
    could not run to a verdict (``gh`` absent, or the API unreachable) — the
    caller then skips rather than failing on a network fault.
    """

    missing: tuple[str, ...]
    indeterminate_reason: str | None = None

    @property
    def ok(self) -> bool:
        return not self.missing and self.indeterminate_reason is None


def _default_run(args: list[str]) -> tuple[int, str]:
    """Run ``gh <args>`` capturing combined stdout+stderr; ``(returncode, text)``.

    Routes through :func:`teatree.utils.run.run_allowed_to_fail` (the subprocess
    chokepoint) with ``expected_codes=None`` — a ``gh api`` 4xx is an expected
    probe outcome, not an error to raise on.
    """
    result = run_allowed_to_fail(["gh", *args], expected_codes=None)
    return result.returncode, f"{result.stdout}\n{result.stderr}"


def _has_signal(text: str, signals: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(signal in lowered for signal in signals)


def probe_token_permissions(slug: str, run: GhRunner | None = None) -> GhTokenProbe:
    """Probe whether ``gh``'s token holds every :data:`REQUIRED_PERMISSION_LABELS` on *slug*.

    ``slug`` is ``owner/repo``. Returns a :class:`GhTokenProbe`. Metadata is
    probed first with a read (``GET repos/{slug}``): if the token cannot even
    read the repo the write probes cannot be interpreted (a no-access token 404s
    on everything), so the metadata failure short-circuits. A non-permission
    failure of the metadata read (network) yields an *indeterminate* result so a
    caller never fails the deploy/doctor on an unreachable API.
    """
    run = run or _default_run
    if shutil.which("gh") is None:
        return GhTokenProbe(missing=(), indeterminate_reason="gh CLI not found on PATH")

    meta_code, meta_out = run([f"repos/{slug}"])
    if meta_code != 0:
        if _has_signal(meta_out, _DENIED_SIGNALS):
            return GhTokenProbe(missing=("metadata: read",))
        return GhTokenProbe(missing=(), indeterminate_reason=f"could not read repos/{slug} (API unreachable?)")

    missing: list[str] = []
    for label, template in _WRITE_PROBES:
        args = [part.format(slug=slug) for part in template]
        _code, out = run(args)
        if _FORBIDDEN_SIGNAL in out.lower():
            missing.append(label)
    return GhTokenProbe(missing=tuple(missing))


__all__ = [
    "REQUIRED_PERMISSION_LABELS",
    "GhRunner",
    "GhTokenProbe",
    "probe_token_permissions",
]
