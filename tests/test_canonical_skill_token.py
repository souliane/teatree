"""Unit tests for the pure skill-name canonicalizer ``_canonical_skill_token``.

The skill-loading gate matches a demanded skill against the loaded set by
canonical token. The canonicalizer is PURE, TOTAL and IDEMPOTENT: it takes
the resolved ``(owned, namespace)`` snapshot as explicit arguments rather
than reading the filesystem itself, so the demand side and the loaded side
always share one snapshot per gate invocation (no environment-dependent
under/over-match from a flaky directory scan).

Contract:

- ``f(f(x)) == f(x)`` for every input (idempotence; fixed point).
- never raises (totality).
- a foreign namespace is preserved VERBATIM, so ``other:review`` can never equal
``t3:review``.
- EMPTY ``owned`` degrades to VERBATIM equality (strict, never wider): a bare
``code`` and a namespaced ``t3:code`` do NOT match. The safe failure mode — it
may re-block (recoverable via the kill-switch / per-call token / circuit
breaker), never satisfies a demand for skill B with skill A.
"""

from hooks.scripts.hook_router import _canonical_skill_token

_NS = "t3"
_OWNED = frozenset({"code", "rules", "review"})

# Inputs spanning every branch: bare-owned, namespaced-owned, foreign,
# path-shaped, ac-* (non-owned), already-namespaced-owned, double-namespace.
_INPUTS = [
    "code",
    "t3:code",
    "other:review",
    "skills/rules/SKILL.md",
    "ac-django",
    "review",
    "t3:review",
    "skills/t3:code/SKILL.md",
    "weird:ns:review",
    "code/",
    "",
    "ns:",
]


class TestIdempotence:
    """``f(f(x)) == f(x)`` for every shape, against both populated and empty owned."""

    def test_fixed_point_populated_owned(self) -> None:
        for raw in _INPUTS:
            once = _canonical_skill_token(raw, _OWNED, _NS)
            twice = _canonical_skill_token(once, _OWNED, _NS)
            assert once == twice, f"not a fixed point for {raw!r}: {once!r} -> {twice!r}"

    def test_fixed_point_empty_owned(self) -> None:
        empty: frozenset[str] = frozenset()
        for raw in _INPUTS:
            once = _canonical_skill_token(raw, empty, _NS)
            twice = _canonical_skill_token(once, empty, _NS)
            assert once == twice, f"not a fixed point for {raw!r}: {once!r} -> {twice!r}"


class TestTotality:
    """The canonicalizer never raises, including degenerate inputs."""

    def test_degenerate_inputs_do_not_raise(self) -> None:
        for raw in ["", "/", ":", "/SKILL.md", "ns:", "a/b/c", "::", "a:b:c"]:
            _canonical_skill_token(raw, _OWNED, _NS)


class TestOwnedBranch:
    """Bare and already-namespaced owned skills canonicalize to ``<ns>:<bare>``."""

    def test_bare_owned_gains_namespace(self) -> None:
        assert _canonical_skill_token("code", _OWNED, _NS) == "t3:code"

    def test_namespaced_owned_is_fixed(self) -> None:
        assert _canonical_skill_token("t3:code", _OWNED, _NS) == "t3:code"

    def test_path_shaped_owned_gains_namespace(self) -> None:
        assert _canonical_skill_token("skills/rules/SKILL.md", _OWNED, _NS) == "t3:rules"


class TestForeignNamespacePreserved:
    """A foreign namespace is preserved VERBATIM and never collides with ours."""

    def test_foreign_namespace_distinct_from_owned(self) -> None:
        # ``review`` is owned, so a bare demand canonicalizes to ``t3:review``.
        # A loaded ``other:review`` must stay ``other:review`` and NOT match.
        assert _canonical_skill_token("review", _OWNED, _NS) != _canonical_skill_token("other:review", _OWNED, _NS)

    def test_foreign_namespace_is_verbatim(self) -> None:
        assert _canonical_skill_token("other:review", _OWNED, _NS) == "other:review"

    def test_last_colon_splits_double_namespace(self) -> None:
        # A foreign ``weird:ns`` prefix is preserved verbatim around the last colon.
        assert _canonical_skill_token("weird:ns:review", _OWNED, _NS) == "weird:ns:review"


class TestNonOwnedStaysBare:
    """A non-owned bare name (supplementary ``ac-*``) stays unqualified."""

    def test_ac_skill_stays_bare(self) -> None:
        assert _canonical_skill_token("ac-django", _OWNED, _NS) == "ac-django"


class TestEmptyOwnedStrictDegrade:
    """EMPTY ``owned`` collapses to verbatim equality — strict, never wider."""

    def test_bare_is_verbatim_when_owned_empty(self) -> None:
        empty: frozenset[str] = frozenset()
        assert _canonical_skill_token("code", empty, _NS) == "code"

    def test_namespaced_is_verbatim_when_owned_empty(self) -> None:
        empty: frozenset[str] = frozenset()
        assert _canonical_skill_token("t3:code", empty, _NS) == "t3:code"

    def test_bare_and_namespaced_do_not_match_when_owned_empty(self) -> None:
        # The safe failure mode: an unreadable owned set must NEVER let a bare
        # ``code`` satisfy a demand spelled ``t3:code`` (or vice versa). It may
        # over-block (recoverable), but it must not falsely match.
        empty: frozenset[str] = frozenset()
        assert _canonical_skill_token("code", empty, _NS) != _canonical_skill_token("t3:code", empty, _NS)
