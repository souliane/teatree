# test-path: cross-cutting — drives deploy/entrypoint.sh against prek_hook (no src mirror).
"""Hook hardening has ONE owner, and the container entrypoint is not a second one.

The init ran `prek install -f` against the SHARED hooks dir and then `sed`-ed the baked
path to `PREK="prek"` — the UNPROBED PATH lookup `harden_hooks` was changed to stop
writing, because it execs whatever PATH resolves first: a bind-mounted host venv on the
container's PATH passes `[ -x ]` and dies `Exec format error`, refusing every push. That
dir is shared with the host, so a second writer there is a second answer to one question,
and the one it wrote is the superseded one.

The entrypoint needs no hardening of its own. `t3 setup` runs immediately after via
`prepare_agent_homes` and reaches the SAME hooks dir — `_installed_clone` walks up from
the vendored package past `vendor/teatree` (which carries no `.git`) to the fork root,
whose common git dir is the one `prek install` wrote — and it now hardens whether or not
the hooks were already present.
"""

import re
from pathlib import Path

ENTRYPOINT = Path(__file__).resolve().parents[1] / "deploy" / "entrypoint.sh"

#: The form the entrypoint used to bake. Unprobed: it names no candidate to check.
_SUPERSEDED_UNPROBED_FORM = 'PREK="prek"'

#: Any in-place edit whose expression touches the hook's PREK assignment.
_SED_TOUCHING_PREK = re.compile(r"^\s*sed\b[^\n]*PREK", re.MULTILINE)

_HOOK_INSTALL = "prek install -f"
_HARDENING_OWNER = "prepare_agent_homes"

#: The bounds of the ``init)`` case arm. An ordering assertion measured over the whole
#: file could be answered by a call in a different arm that never runs on the init path.
#: Scoping to this arm is what makes the assertion be about the call that actually
#: repairs what ``prek install -f`` just baked.
_INIT_ARM_OPEN = "\ninit)\n"
_CASE_ARM_CLOSE = "\n    ;;\n"


def _body() -> str:
    return ENTRYPOINT.read_text(encoding="utf-8")


def _executable_lines() -> str:
    """The script with whole-line comments dropped — naming the old form in prose is the point."""
    return "\n".join(line for line in _body().splitlines() if not line.lstrip().startswith("#"))


def _init_arm() -> str:
    """The ``init)`` arm alone — the only path that runs ``prek install -f``.

    Raises rather than degrading to the whole body: a restructure that renames the arm
    has to fail this file loudly, because a guard that silently widens back to the full
    script is the vacuity being fixed here.
    """
    body = _body()
    start = body.index(_INIT_ARM_OPEN) + len(_INIT_ARM_OPEN)
    return body[start : body.index(_CASE_ARM_CLOSE, start)]


def test_the_entrypoint_rewrites_no_prek_assignment_of_its_own() -> None:
    found = _SED_TOUCHING_PREK.findall(_body())

    assert found == [], f"a second hook-hardening implementation lives in the entrypoint:\n{found}"


def test_no_executable_line_emits_the_superseded_unprobed_form() -> None:
    """Broader than the sed guard: a heredoc or an append writes it just as well."""
    assert _SUPERSEDED_UNPROBED_FORM not in _executable_lines(), (
        f"the entrypoint emits {_SUPERSEDED_UNPROBED_FORM} into hooks the host also reads — "
        "that execs whatever PATH resolves first, which is the refusal this fix removed"
    )


def test_the_slice_under_test_is_the_init_arm_and_only_it() -> None:
    """The instrument before the measurement: a mis-sliced arm would certify anything."""
    arm = _init_arm()

    assert "init_preflight" in arm, "the slice is not the `init)` arm"
    assert "exec t3 worker" not in arm, "the slice ran past `init)` into `worker)` — the bound is wrong"


def test_the_hook_install_is_followed_by_the_setup_that_hardens() -> None:
    """Anti-vacuity: dropping the sed is only safe because the real owner runs after it.

    Scoped to ``init)``, the arm that RUNS the install, so it is that arm's OWN call which
    has to repair it. Measured over the whole file the assertion cannot fail: delete init's
    call and ``index`` falls through to ``worker)``'s — later in the text, so still "after"
    the install, but never reached on this path — and every test here stays green.
    """
    arm = _init_arm()
    hardening_call = f"    {_HARDENING_OWNER}\n"

    assert _HOOK_INSTALL in arm, "the `init)` arm no longer runs the hook install this guard is about"
    assert hardening_call in arm, (
        f"nothing in the `init)` arm hardens the hooks `prek install -f` just baked — "
        f"{_HARDENING_OWNER} runs in other arms, none of which is on the init path"
    )
    assert arm.index(_HOOK_INSTALL) < arm.index(hardening_call), (
        f"{_HARDENING_OWNER} runs BEFORE `prek install -f` in `init)`, so the baked path it "
        "would have repaired is written after it"
    )
