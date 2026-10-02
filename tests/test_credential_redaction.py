# test-path: cross-cutting
# Exercises hooks/scripts/credential_redaction.py, which has no src/teatree mirror.
"""No credential reaches agent-facing text, and no benign text is erased to get there.

Seven rounds each closed the shape they were handed and shipped while a
neighbouring one leaked, because a pair MATCHER decides where a value ends and
that decision is what kept being wrong. The scanner under test decides nothing:
it PARTITIONS, and the partition is computed before and independently of any
classification.

So the corpus is judged three ways. Once against the scanner, which must carry no
credential out. Once against five deliberately WEAKENED scanners — each the same
partitions with exactly one classification rule removed — each of which must leak
on cells of the stage its rule governs and on ZERO cells of any other stage: a
leak caught somewhere is caught, but not on its own axis. And once against W0, the
harness with nothing removed, which must be byte-identical to the scanner on every
cell — without that control a variant's leak could be the harness's own bug.
"""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Final, Protocol

import pytest

import hooks.scripts.credential_redaction as redaction
import hooks.scripts.foreign_branch_push_git as git_probes

REDACTED: Final[str] = "<redacted>"
# Stated here rather than imported: the marker's own text is the contract, and a
# marker read out of the module it pins would agree with any spelling of it.
_TRUNCATED: Final[str] = f"<truncated-at-{redaction._MAX_SCANNED_CHARS}-chars>"
# Deliberately low-entropy and self-describing: a realistic-looking token value
# here is what the repo's own secret scanner exists to refuse.
_T: Final[str] = "not-a-real-token"


# ── The corpus ─────────────────────────────────────────────────────
#
# Four axes, each present because a single-axis matrix cannot see the defect the
# axis exists for: NAMES (round 7 kept 43 of them verbatim), VALUES (round 5 split
# one on its own base64 alphabet and printed the tail), JOINS (round 7's six were
# all outside the name class, so 0/141 forms leaked on any of them), and PREFIXES
# (round 7's leak was CONDITIONAL on the first pair's classification, which a
# single-prefix matrix cannot reach).

_NAMES: Final[tuple[tuple[str, str], ...]] = (
    ("token", "plain"),
    ("private-token", "hyphenated"),
    ("TOKEN", "upper-cased"),
    ("accessToken", "camel-hump"),
    ("%70%61%73%73%77%6F%72%64", "percent-encoded"),
    ("api_keys", "plural"),
    ("wombat_grommet", "a-coinage-no-enumeration-holds"),
    ("zzz", "a-three-letter-coinage"),
    ("password2", "digit-glued"),
    ("X-Amz-Signature", "vendor-prefixed"),
    # Round 7 filed this under "redacted at a stated legibility cost — an
    # identifier". An AWS access key id is half a credential pair, so redacting
    # it is a safety win that was booked as a loss.
    ("AWSAccessKeyId", "an-identifier-that-is-half-a-credential"),
    ("cli%2Dsecret", "a-mid-string-escape"),
    # Every name round 7's allowlist printed verbatim. `state` is the sharpest:
    # an OAuth CSRF nonce is conventionally spelled `state`, and no name-keyed
    # predicate can tell that from a forge's `state=opened`.
    ("ref", "an-allowlisted-ref"),
    ("state", "an-allowlisted-state"),
    ("path", "an-allowlisted-path"),
    ("id", "an-allowlisted-id"),
    ("q", "an-allowlisted-q"),
    ("rc", "an-allowlisted-rc"),
)

_ALPHABET_CHUNKS: Final[tuple[str, ...]] = (
    "alphaAAA",
    "alphaBBB",
    "alphaCCC",
    "alphaDDD",
    "alphaEEE",
    "alphaFFF",
    "alphaGGG",
)
# The credential extent's own alphabet: base64, URL-safe base64, percent-encoding
# and JWT all put these INSIDE the secret. Splitting a value here is what published
# `<redacted>+Qw==` in round 5.
_VALUE_ALPHABET: Final[str] = "alphaAAA/alphaBBB+alphaCCC%2FalphaDDD:alphaEEE~alphaFFF^alphaGGG=="

_CLOSED_SEPARATOR_CHUNKS: Final[tuple[str, ...]] = (
    "sepAAA",
    "sepBBB",
    "sepCCC",
    "sepDDD",
    "sepEEE",
    "sepFFF",
    "sepGGG",
)
# R1/R2, named rather than discovered: a credential carrying ANY run-boundary
# character keeps its tail — whitespace and quotes are only the shapes git's own
# prose puts there (`fatal: … '<url>': 403`), and a URL must percent-encode them.
_RESIDUAL_SEPARATOR_CHUNKS: Final[tuple[str, ...]] = ("sepHHH", "sepIII", "sepJJJ")
_VALUE_SEPARATORS: Final[str] = "sepAAA&sepBBB#sepCCC,sepDDD;sepEEE|sepFFF?sepGGG sepHHH'sepIII`sepJJJ"

_VALUES: Final[tuple[tuple[str, tuple[str, ...], str], ...]] = (
    (_T, (_T,), "plain"),
    (_VALUE_ALPHABET, _ALPHABET_CHUNKS, "carrying-the-value-alphabet"),
    (_VALUE_SEPARATORS, _CLOSED_SEPARATOR_CHUNKS, "carrying-the-hard-separators"),
)

_JOINS: Final[tuple[tuple[str, str], ...]] = (
    *(
        (char, f"name-class-{label}")
        for char, label in (
            ("-", "hyphen"),
            (".", "dot"),
            ("_", "underscore"),
            ("9", "digit"),
            ("=", "equals"),
            ("a", "letter"),
            ("%26", "escaped-ampersand"),
            ("%2F", "escaped-slash"),
            ("%20", "escaped-space"),
        )
    ),
    *(
        (char, f"separator-{label}")
        for char, label in (
            ("&", "ampersand"),
            (",", "comma"),
            (";", "semicolon"),
            ("|", "pipe"),
            ("?", "question-mark"),
            ("#", "hash"),
        )
    ),
    *(
        (char, f"value-alphabet-{label}")
        for char, label in (
            ("/", "slash"),
            ("+", "plus"),
            ("%", "percent"),
            (":", "colon"),
            ("~", "tilde"),
            ("^", "caret"),
        )
    ),
)

# A form may not re-establish its own boundary: 55 of round 7's 63 forms began with
# `?`, so the row's own `?` became the boundary and the join was never exercised.
_PREFIXES: Final[tuple[tuple[str, str], ...]] = (
    ("", "bare"),
    ("ref=main", "after-an-allowlisted-pair"),
    ("zzz=main", "after-a-coinage-pair"),
)


@dataclass(frozen=True, slots=True)
class _Cell:
    """One corpus cell: its text, what must not survive it, and where it sits on each axis."""

    text: str
    closed: tuple[str, ...]
    prefix: str
    axes: dict[str, str]
    label: str


_CORPUS: Final[tuple[_Cell, ...]] = tuple(
    _Cell(
        text=f"{prefix}{join}{name}={value}",
        closed=closed,
        prefix=prefix,
        axes={"NAME": name_id, "VALUE": value_id, "JOIN": join_id, "PREFIX": prefix_id},
        label=f"{name_id}-{value_id}-{join_id}-{prefix_id}",
    )
    for prefix, prefix_id in _PREFIXES
    for join, join_id in _JOINS
    for name, name_id in _NAMES
    for value, closed, value_id in _VALUES
)
_CORPUS_CELLS: Final[list] = [pytest.param(cell.text, cell.closed, cell.prefix, id=cell.label) for cell in _CORPUS]


# ── The userinfo sub-corpus ────────────────────────────────────────
#
# Not one of the 3402 pair cells contains `://`, so the userinfo rule was
# unreachable from every one of them and a scanner missing it read as caught. The
# contract is SCHEME-CONDITIONAL — on the http family the whole userinfo goes,
# under any other scheme a lone userinfo is a login name and only the half past the
# first `:` does — so each cell puts its credential where that scheme redacts it.

_HTTP_SCHEMES: Final[tuple[tuple[str, str], ...]] = (
    ("https", "https"),
    ("http", "http"),
    ("HTTPS", "upper-cased"),
    ("git::https", "remote-helper-prefixed"),
)
_HTTP_SHAPES: Final[tuple[tuple[str, str], ...]] = (
    (_T, "a-lone-userinfo"),
    (f"oauth2:{_T}", "a-user-and-its-token"),
    (f":{_T}", "an-empty-user"),
    (f"{_T}:x-oauth-basic", "the-token-as-the-username"),
    (f"u%40corp:{_T}", "an-escaped-at-in-the-username"),
    (f"tok%3A{_T}", "an-escaped-colon-is-no-separator"),
)
_OTHER_SCHEMES: Final[tuple[tuple[str, str], ...]] = (("ssh", "ssh"), ("git+ssh", "git-over-ssh"))
_OTHER_SHAPES: Final[tuple[tuple[str, str], ...]] = (
    (f"u:{_T}", "a-login-and-its-password"),
    (f"git:{_T}", "the-conventional-git-login"),
)
_TRAILERS: Final[tuple[tuple[str, str], ...]] = (
    ("h.invalid/o/r.git", "a-plain-host"),
    ("h.invalid:8443/o/r.git", "a-host-port"),
    ("[fd00::1]:8443/o/r.git", "an-ipv6-authority"),
    ("h.invalid/o/r?a=b", "a-query-after-the-host"),
    # ONE run, TWO authorities: the userinfo rule loops, and nothing else here asks it to.
    (f"h.invalid/o/r.git-https://c:{_T}@h2.invalid/y", "a-second-authority-in-the-run"),
)

_USERINFO_CORPUS: Final[tuple[_Cell, ...]] = tuple(
    _Cell(
        text=f"{scheme}://{shape}@{trailer}",
        closed=(_T,),
        prefix="",
        axes={f"{family}_SCHEME": scheme_id, f"{family}_SHAPE": shape_id, "TRAILER": trailer_id},
        label=f"{family.lower()}-{scheme_id}-{shape_id}-{trailer_id}",
    )
    for family, schemes, shapes in (("HTTP", _HTTP_SCHEMES, _HTTP_SHAPES), ("OTHER", _OTHER_SCHEMES, _OTHER_SHAPES))
    for scheme, scheme_id in schemes
    for shape, shape_id in shapes
    for trailer, trailer_id in _TRAILERS
)
_USERINFO_CELLS: Final[list] = [pytest.param(cell.text, id=cell.label) for cell in _USERINFO_CORPUS]

_PAIR_AXES: Final[dict[str, frozenset[str]]] = {
    "NAME": frozenset(label for _, label in _NAMES),
    "VALUE": frozenset(label for _, _closed, label in _VALUES),
    "JOIN": frozenset(label for _, label in _JOINS),
    "PREFIX": frozenset(label for _, label in _PREFIXES),
}
_USERINFO_AXES: Final[dict[str, frozenset[str]]] = {
    "HTTP_SCHEME": frozenset(label for _, label in _HTTP_SCHEMES),
    "HTTP_SHAPE": frozenset(label for _, label in _HTTP_SHAPES),
    "OTHER_SCHEME": frozenset(label for _, label in _OTHER_SCHEMES),
    "OTHER_SHAPE": frozenset(label for _, label in _OTHER_SHAPES),
    "TRAILER": frozenset(label for _, label in _TRAILERS),
}
_STAGES: Final[dict[str, tuple[tuple[_Cell, ...], dict[str, frozenset[str]]]]] = {
    "segment": (_CORPUS, _PAIR_AXES),
    "userinfo": (_USERINFO_CORPUS, _USERINFO_AXES),
}


# ── The falsification harness ──────────────────────────────────────


def _partition(text: str, boundaries: str) -> list[tuple[str, str]]:
    pieces: list[tuple[str, str]] = []
    start = 0
    for index, char in enumerate(text):
        if char in boundaries:
            pieces.append((text[start:index], char))
            start = index + 1
    pieces.append((text[start:], ""))
    return pieces


class _Classify(Protocol):
    def __call__(self, segment: str, *, tainted: bool) -> tuple[str, bool]: ...


_AUTHORITY_RE: Final[re.Pattern[str]] = re.compile(r"(?P<scheme>[A-Za-z0-9+.-]*)://(?P<userinfo>[^/?#@]*)@")
# An authority a run boundary cut short: what follows `://` may or may not be a
# userinfo, and the scanner refuses to publish either way.
_CUT_SHORT_RE: Final[re.Pattern[str]] = re.compile(r"://[^/?#@]+$")


def _redact_authority(match: re.Match[str]) -> str:
    login, colon, _ = match["userinfo"].partition(":")
    if match["scheme"].lower().endswith(("http", "https")):
        return f"{match['scheme']}://{REDACTED}@"
    return f"{match['scheme']}://{login}:{REDACTED}@" if colon else match[0]


def _faithful_userinfo(run: str) -> str:
    """The scanner's userinfo rule, reimplemented: the harness calls no production code."""
    return _CUT_SHORT_RE.sub(f"://{REDACTED}", _AUTHORITY_RE.sub(_redact_authority, run))


def _w6_first_authority_only(run: str) -> str:
    return _CUT_SHORT_RE.sub(f"://{REDACTED}", _AUTHORITY_RE.sub(_redact_authority, run, count=1))


def _no_userinfo_rule(run: str) -> str:
    return run


def _weakened(text: str, classify: _Classify, userinfo: Callable[[str], str]) -> str:
    """The scanner's three partitions, with ONE classification rule swapped out.

    The partitions never vary: they are what the design fixes, and a variant that
    changed one would be testing a different mechanism rather than one rule.
    """
    kept: list[str] = []
    for run, boundary in _partition(text, " \t\n\r\v\f\x00\xa0'\"`"):
        tainted = False
        pending = ""
        for segment, separator in _partition(userinfo(run), "&,;|?#"):
            emitted, tainted = classify(segment, tainted=tainted)
            kept.append(f"{pending}{emitted}")
            pending = separator
        kept.append(boundary)
    return "".join(kept)


def _scanner(classify: _Classify, userinfo: Callable[[str], str]) -> Callable[[str], str]:
    return partial(_weakened, classify=classify, userinfo=userinfo)


def _w0_faithful(segment: str, *, tainted: bool) -> tuple[str, bool]:
    """The scanner's own segment rule, reimplemented: W0 removes nothing."""
    name, equals, _ = segment.partition("=")
    if equals:
        return f"{name}={REDACTED}", True
    return (REDACTED if tainted else segment), tainted


def _w1_no_taint(segment: str, *, tainted: bool) -> tuple[str, bool]:
    name, equals, _ = segment.partition("=")
    return (f"{name}={REDACTED}", True) if equals else (segment, tainted)


def _w2_value_ends_at_a_separator(segment: str, *, tainted: bool) -> tuple[str, bool]:
    name, equals, value = segment.partition("=")
    if not equals:
        return (REDACTED if tainted else segment), tainted
    cut = min((value.find(char) for char in "/+%" if char in value), default=len(value))
    return f"{name}={REDACTED}{value[cut:]}", True


def _w3_first_pair_only(segment: str, *, tainted: bool) -> tuple[str, bool]:
    name, equals, _ = segment.partition("=")
    if equals and not tainted:
        return f"{name}={REDACTED}", True
    return segment, tainted


_W4_BENIGN: Final[frozenset[str]] = frozenset({"rc", "ref", "sha", "tag", "path", "id", "q", "state", "version"})


def _w4_benign_names(segment: str, *, tainted: bool) -> tuple[str, bool]:
    name, equals, _ = segment.partition("=")
    if not equals:
        return (REDACTED if tainted else segment), tainted
    if name.lower() in _W4_BENIGN:
        return segment, tainted
    return f"{name}={REDACTED}", True


@dataclass(frozen=True, slots=True)
class _Variant:
    """One scanner with exactly one rule removed, and what the corpus must say about it."""

    label: str
    scan: Callable[[str], str]
    stage: str
    named_cell: str
    leaked: tuple[str, ...]
    load_bearing: dict[str, frozenset[str]]


_VARIANTS: Final[tuple[_Variant, ...]] = (
    _Variant(
        label="W1_no_taint",
        scan=_scanner(_w1_no_taint, _faithful_userinfo),
        stage="segment",
        named_cell=f"?token=AAA&BBB{_T}",
        leaked=(_T,),
        load_bearing={"VALUE": frozenset({"carrying-the-hard-separators"})},
    ),
    _Variant(
        label="W2_value_ends_at_separator",
        scan=_scanner(_w2_value_ends_at_a_separator, _faithful_userinfo),
        stage="segment",
        named_cell=f"?client_secret=aB3/xY9+{_T}",
        leaked=(_T,),
        load_bearing={},
    ),
    _Variant(
        label="W3_first_pair_only",
        scan=_scanner(_w3_first_pair_only, _faithful_userinfo),
        stage="segment",
        named_cell=f"?ref=main&private_token={_T}",
        leaked=(_T,),
        load_bearing={},
    ),
    _Variant(
        label="W4_benign_names",
        scan=_scanner(_w4_benign_names, _faithful_userinfo),
        stage="segment",
        named_cell=f"?ref={_T}&state={_T}&rc={_T}",
        leaked=(_T,),
        load_bearing={},
    ),
    _Variant(
        label="W5_no_userinfo",
        scan=_scanner(_w0_faithful, _no_userinfo_rule),
        stage="userinfo",
        named_cell=f"https://{_T}@h.invalid/o/r.git",
        leaked=(_T,),
        load_bearing={},
    ),
    _Variant(
        label="W6_first_authority_only",
        scan=_scanner(_w0_faithful, _w6_first_authority_only),
        stage="userinfo",
        named_cell=f"https://u:p@h1.invalid/x-https://c:{_T}@h2.invalid/y",
        leaked=(_T,),
        load_bearing={"TRAILER": frozenset({"a-second-authority-in-the-run"})},
    ),
)
_VARIANT_IDS: Final[list[str]] = [variant.label for variant in _VARIANTS]


# Every corpus cell carries a TERMINATED authority, so the rule for one a run boundary
# cut short is unreachable from all of them and W0 would agree with any spelling of it.
_CUT_SHORT_SHAPES: Final[tuple[str, ...]] = (
    f"https://u:{_T}",
    f"https://{_T}",
    "https://h.invalid",
    f"ssh://u:{_T}",
    f"https://a@h.invalid/x-https://u:{_T}",
    "https://",
)


def _caught(variant: _Variant, corpus: tuple[_Cell, ...]) -> list[_Cell]:
    return [cell for cell in corpus if any(fragment in variant.scan(cell.text) for fragment in cell.closed)]


class TestTheCorpusDiscriminates:
    r"""Five scanners, each missing one rule, against the two corpora — and the control that they mean anything.

    A corpus every implementation passes is a number. Each variant below is a real
    redactor a weaker corpus would have certified, and what is pinned is not merely
    that SOME cell catches it: it is caught on its own stage, on zero cells of the
    other, and on exactly the axis members measured here.

    What this matrix still cannot see, stated rather than implied. The partitions
    are fixed by construction — their boundary sets are literals here, so no variant
    can express "drop a boundary character", and W0 catches such an edit only if it
    changes output on some cell: no cell holds `\t \n \r \v \f \x00 \xa0 "`, so
    dropping any of those stays invisible. Over-redaction is unfalsifiable too —
    every assertion here is "fragment absent", which a scanner that erased
    everything would pass; only `_MUST_KEEP`, `_REDACTED_AT_A_PRICED_COST` and the
    surviving-prefix assertion guard that direction. The 64 KiB cap is no axis
    either: every cell is far under it, and `TestTheInputCap` is its only guard.
    """

    @pytest.mark.parametrize("variant", _VARIANTS, ids=_VARIANT_IDS)
    def test_a_weakened_scanner_leaks_on_its_named_cell(self, variant: _Variant) -> None:
        weakened = variant.scan(variant.named_cell)
        assert any(fragment in weakened for fragment in variant.leaked), (
            f"{variant.label} was supposed to leak on `{variant.named_cell}` and did not, so it pins nothing"
        )
        assert all(fragment not in redaction.redact_credentials(variant.named_cell) for fragment in variant.leaked), (
            f"the control: the real scanner must carry no credential out of `{variant.named_cell}`"
        )

    @pytest.mark.parametrize("variant", _VARIANTS, ids=_VARIANT_IDS)
    def test_a_variant_leaks_only_on_its_own_stage(self, variant: _Variant) -> None:
        """Caught on another stage's cells is caught somewhere, not on the axis the variant removes."""
        for stage, (corpus, _axes) in _STAGES.items():
            caught = _caught(variant, corpus)
            if stage == variant.stage:
                assert caught, f"no {stage} cell catches {variant.label}, so the corpus certifies a scanner that leaks"
            else:
                assert not caught, (
                    f"{variant.label} drops a {variant.stage} rule yet leaks on {len(caught)} {stage} cells"
                )

    @pytest.mark.parametrize("variant", _VARIANTS, ids=_VARIANT_IDS)
    def test_the_corpus_catches_it_only_where_the_profile_says(self, variant: _Variant) -> None:
        """The measured attribution, pinned per axis — a member added without re-measuring goes red."""
        corpus, axes = _STAGES[variant.stage]
        caught = _caught(variant, corpus)
        for axis, members in axes.items():
            observed = frozenset(cell.axes[axis] for cell in caught if axis in cell.axes)
            assert observed == variant.load_bearing.get(axis, members), f"{variant.label} on axis {axis}"

    def test_the_harness_with_no_rule_removed_is_the_scanner(self) -> None:
        """W0, the control that makes every leak above evidence rather than a harness bug."""
        faithful = _scanner(_w0_faithful, _faithful_userinfo)
        mismatched = [
            text
            for text in (*(cell.text for cell in (*_CORPUS, *_USERINFO_CORPUS)), *_CUT_SHORT_SHAPES)
            if faithful(text) != redaction.redact_credentials(text)
        ]
        assert not mismatched, f"the harness disagrees with the scanner on {len(mismatched)} cells: {mismatched[:3]}"


# ── The scan's cost ────────────────────────────────────────────────

_KIB64: Final[int] = 64 * 1024
# Measured, not guessed: the healthy worst is `quote-run` at 31.7 ms best-of-5 on a
# box contended by this suite's own `-n auto` workers, and round 7's RED here is
# 13362 ms. 250 ms sits 7.9x above the one and 53x below the other — the growth
# ratio catches a merely-quadratic scan, so the absolute half need only catch a
# uniformly slow one.
_ABSOLUTE_BUDGET_S: Final[float] = 0.250
_GROWTH_CEILING: Final[float] = 8.0
_ABSOLUTE_TRIALS: Final[int] = 5
# The ratio of two sub-millisecond measurements is the noise-sensitive half: a
# neighbouring xdist worker read 4.0x as 9.8x at best-of-3.
_GROWTH_TRIALS: Final[int] = 9
# Round 7's own performance test built its pathology BEFORE the `=` only, which is
# why it stayed green at 8934 ms: the value extent it scanned quadratically was
# reached only AFTER one. Both positions are built here.
_SHAPES: Final[tuple[tuple[str, str, str], ...]] = (
    ("percent-escape-run", "%2F", ""),
    ("mixed-escape-run", "a%2F", ""),
    ("plain-run", "AAA", ""),
    ("scheme-shaped-run", "https://a", ""),
    ("pair-run", "&b=", ""),
    ("at-run", "a@", ""),
    ("quote-run", "'x", ""),
    ("percent-escape-run-then-a-credential", "%2F", f"&token={_T}"),
)


def _after_the_equals(unit: str, suffix: str, budget: int) -> str:
    head = "?zzz="
    return f"{head}{unit * max(1, (budget - len(head) - len(suffix)) // len(unit))}{suffix}"


def _before_the_equals(unit: str, suffix: str, budget: int) -> str:
    tail = f"?token={_T}{suffix}"
    return f"{unit * max(1, (budget - len(tail)) // len(unit))}{tail}"


def _fastest(text: str, trials: int) -> float:
    def once() -> float:
        started = time.perf_counter()
        redaction.redact_credentials(text)
        return time.perf_counter() - started

    return min(once() for _ in range(trials))


_POSITIONS: Final[tuple[tuple[str, Callable[[str, str, int], str], str], ...]] = (
    ("after-the-equals", _after_the_equals, f"?zzz={REDACTED}"),
    ("before-the-equals", _before_the_equals, f"?token={REDACTED}"),
)
_COST_CELLS: Final[list] = [
    pytest.param(build, unit, suffix, marker, id=f"{shape_id}-{position_id}")
    for position_id, build, marker in _POSITIONS
    for shape_id, unit, suffix in _SHAPES
]


class TestTheScanIsLinearWithThePathologyAfterTheEquals:
    """Both bounds, because either alone certifies a quadratic scan.

    An absolute budget on a fast box passes a mildly quadratic implementation; a
    growth ratio alone passes one that is uniformly slow. The anti-vacuity control
    is the third: an implementation that bails out early is fast for a reason
    timing cannot see.
    """

    @pytest.mark.parametrize(("build", "unit", "suffix", "marker"), _COST_CELLS)
    def test_the_scan_is_linear_with_the_pathology_after_the_equals(
        self, build: Callable[[str, str, int], str], unit: str, suffix: str, marker: str
    ) -> None:
        large = build(unit, suffix, _KIB64)
        assert len(large) <= _KIB64, "the shape must fit under the input cap, or the cap is what made it fast"
        elapsed = _fastest(large, _ABSOLUTE_TRIALS)
        assert elapsed < _ABSOLUTE_BUDGET_S, (
            f"{elapsed * 1000:.1f}ms to scan {len(large)} chars, against a {_ABSOLUTE_BUDGET_S * 1000:.0f}ms budget"
        )
        redacted = redaction.redact_credentials(large)
        assert marker in redacted, "the control: a scan that bailed out early is fast for a reason timing cannot see"
        if suffix:
            assert redacted.endswith(f"&token={REDACTED}"), (
                "the mandatory control: the scan must reach the credential at the far end of the pathology"
            )
        small = _fastest(build(unit, suffix, 4000), _GROWTH_TRIALS)
        big = _fastest(build(unit, suffix, 16000), _GROWTH_TRIALS)
        assert big / small < _GROWTH_CEILING, (
            f"4x the input cost {big / small:.1f}x the time — linear is ~4, quadratic ~16"
        )


# ── One test per blocker whose RED is the measured leak ────────────


class TestTheLeaksRoundSevenShipped:
    """Each of these reproduces a leak measured against the shipped head."""

    @pytest.mark.parametrize("escape", ["%26", "%2F", "%3D", "%20"])
    def test_a_well_formed_escape_before_a_name_does_not_hide_the_pair(self, escape: str) -> None:
        redacted = redaction.redact_credentials(f"https://h.invalid/o/r.git?ref=main{escape}private_token={_T}")
        assert _T not in redacted
        assert REDACTED in redacted

    @pytest.mark.parametrize(
        "text",
        [
            f"https://h.invalid/o/r.git?ref=1%private_token={_T}",
            f"https://h.invalid/o/r.git?ref=main%ZZprivate_token={_T}",
        ],
    )
    def test_a_stray_or_non_hex_percent_does_not_hide_the_pair(self, text: str) -> None:
        """The control: the guard round 7 narrowed cannot be re-narrowed to pass the rows above."""
        redacted = redaction.redact_credentials(text)
        assert _T not in redacted
        assert REDACTED in redacted

    @pytest.mark.parametrize("join", ["-", ".", "_", "9", "=", "a", "%2F"])
    def test_a_benign_value_does_not_swallow_the_credential_behind_it(self, join: str) -> None:
        redacted = redaction.redact_credentials(f"?ref=main{join}private_token={_T}")
        assert _T not in redacted

    @pytest.mark.parametrize("join", ["-", ".", "_", "9", "=", "a", "%2F", "&", ",", ";", "|", "?", "#"])
    def test_the_same_join_after_a_coinage_pair_also_redacts(self, join: str) -> None:
        """The control that separates a fix which redacts CORRECTLY from one that redacts everything."""
        redacted = redaction.redact_credentials(f"?zzz=main{join}private_token={_T}")
        assert _T not in redacted
        assert redacted.startswith("?zzz="), "the parameter name must survive so the refusal stays actionable"

    def test_a_run_after_a_redacted_value_publishes_no_fragment(self) -> None:
        redacted = redaction.redact_credentials(f"?token={_VALUE_SEPARATORS}")
        for chunk in _CLOSED_SEPARATOR_CHUNKS:
            assert chunk not in redacted, f"a separator inside the value published `{chunk}`"

    @pytest.mark.parametrize("text", ["ssh://git@h.invalid/o/r.git", "git@h.invalid:o/r.git", "exit 128: fatal: no"])
    def test_a_run_carrying_no_pair_stays_byte_identical(self, text: str) -> None:
        """The control for the row above: a scrub that ate these would satisfy every leak assertion."""
        assert redaction.redact_credentials(text) == text


# ── The corpus, executed ───────────────────────────────────────────


class TestEveryCorpusCellCarriesNoCredentialOut:
    @pytest.mark.parametrize(("text", "closed", "prefix"), _CORPUS_CELLS)
    def test_the_cell_carries_no_credential_out(self, text: str, closed: tuple[str, ...], prefix: str) -> None:
        redacted = redaction.redact_credentials(text)
        for fragment in closed:
            assert fragment not in redacted, f"`{fragment}` survived into `{redacted}`"
        assert REDACTED in redacted, "nothing was redacted, so the assertion above proved nothing"
        if prefix:
            assert redacted.startswith(prefix.split("=", 1)[0]), "the leading parameter name must survive"

    @pytest.mark.parametrize("text", _USERINFO_CELLS)
    def test_the_userinfo_cell_carries_no_credential_out(self, text: str) -> None:
        redacted = redaction.redact_credentials(text)
        assert _T not in redacted, f"the credential survived into `{redacted}`"
        assert REDACTED in redacted, "nothing was redacted, so the assertion above proved nothing"

    def test_the_named_residuals_are_residuals_and_not_coverage(self) -> None:
        """R1/R2 recorded as an outcome, so a future round cannot read the row above as coverage."""
        redacted = redaction.redact_credentials(f"?token={_VALUE_SEPARATORS}")
        assert all(chunk in redacted for chunk in _RESIDUAL_SEPARATOR_CHUNKS)

    @pytest.mark.parametrize("boundary", ["\v", "\f", "\x00", "\xa0"])
    def test_any_run_boundary_inside_a_value_publishes_its_tail(self, boundary: str) -> None:
        """R1/R2 as MEASURED: every run boundary resets taint, not only whitespace and quotes."""
        redacted = redaction.redact_credentials(f"?token=AAA&BBB{boundary}{_T}")
        assert redacted == f"?token={REDACTED}&{REDACTED}{boundary}{_T}"


_MUST_REDACT_USERINFO: Final[list] = [
    pytest.param(f"https://oauth2:{_T}@h.invalid/o/r.git", f"https://{REDACTED}@h.invalid/o/r.git", id="user:token@"),
    pytest.param(f"https://{_T}@h.invalid/o/r.git", f"https://{REDACTED}@h.invalid/o/r.git", id="lone-userinfo"),
    pytest.param(f"http://{_T}@h.invalid/o/r.git", f"http://{REDACTED}@h.invalid/o/r.git", id="lone-userinfo-http"),
    pytest.param(f"HTTPS://{_T}@h.invalid/o/r.git", f"HTTPS://{REDACTED}@h.invalid/o/r.git", id="upper-cased-scheme"),
    pytest.param(f"https://:{_T}@h.invalid/o/r.git", f"https://{REDACTED}@h.invalid/o/r.git", id="empty-user"),
    # GitHub's own documented PAT push URL, where the TOKEN is the Basic-auth
    # USERNAME. Keeping the username on the reasoning that the password half is the
    # secret printed the token verbatim, so on `http(s)` nothing in a userinfo survives.
    pytest.param(
        f"https://{_T}:x-oauth-basic@github.invalid/o/r.git",
        f"https://{REDACTED}@github.invalid/o/r.git",
        id="pat-as-the-basic-auth-username",
    ),
    pytest.param(
        f"https://x-access-token:{_T}@github.invalid/o/r.git",
        f"https://{REDACTED}@github.invalid/o/r.git",
        id="github-app-x-access-token",
    ),
    pytest.param(
        f"https://{_T}:@github.invalid/o/r.git",
        f"https://{REDACTED}@github.invalid/o/r.git",
        id="pat-username-with-an-empty-password",
    ),
    pytest.param(
        f"git::https://{_T}@h.invalid/o/r.git",
        f"git::https://{REDACTED}@h.invalid/o/r.git",
        id="git::https-transport",
    ),
    pytest.param(
        f"https://{_T}@h.invalid:8443/o/r.git",
        f"https://{REDACTED}@h.invalid:8443/o/r.git",
        id="lone-userinfo-with-port",
    ),
    pytest.param(
        f"https://{_T}@[fd00::1]:8443/o/r.git",
        f"https://{REDACTED}@[fd00::1]:8443/o/r.git",
        id="lone-userinfo-ipv6-authority",
    ),
    pytest.param(
        f"https://u%40corp:{_T}@h.invalid/o/r", f"https://{REDACTED}@h.invalid/o/r", id="escaped-at-in-the-username"
    ),
    pytest.param(
        f"https://tok%3A{_T}@h.invalid/o/r", f"https://{REDACTED}@h.invalid/o/r", id="escaped-colon-is-no-separator"
    ),
    # Under any other scheme a lone userinfo is a login name, so only the half after
    # the first `:` goes and `ssh://git@host` stays byte-identical.
    pytest.param(
        f"ssh://u:{_T}@h.invalid/o/r.git", f"ssh://u:{REDACTED}@h.invalid/o/r.git", id="ssh-keeps-its-login-name"
    ),
    pytest.param(
        f"https://{_T}@h.invalid/a.git?token={_T}",
        f"https://{REDACTED}@h.invalid/a.git?token={REDACTED}",
        id="userinfo-and-query-together",
    ),
]
# A login name is not a credential, and neither is a host port or an exit code. A
# scrub that ate these would satisfy every leak assertion above and leave a refusal
# nobody can act on — which is the whole reason the refusal quotes the URL.
_MUST_KEEP: Final[list] = [
    pytest.param("ssh://git@h.invalid/o/r.git", id="ssh-login-name"),
    pytest.param("git@h.invalid:o/r.git", id="scp-like-login-name"),
    pytest.param("git://h.invalid/o/r.git", id="git-scheme"),
    pytest.param("https://h.invalid:8080/o/r.git", id="host-port-is-not-a-userinfo"),
    pytest.param("https://[fd00::1]:8443/o/r.git", id="ipv6-authority-without-userinfo"),
    pytest.param("exit 128: fatal: not a git repository", id="the-gates-own-exit-code"),
    pytest.param("exit 0", id="the-gates-own-zero-exit"),
    pytest.param("fatal: 'origin' does not appear to be a git repository", id="plain-git-prose"),
    pytest.param("`git ls-remote --heads origin refs/heads/ac/x` (run in `/tmp/w`)", id="a-quoted-probe-argv"),
]
# The cost of a partition with no allowlist, priced in the corpus rather than
# discovered in a refusal. Every parameter NAME still survives; the host, path,
# branch, exit code, remedy and git's prose outside a pair are unchanged.
_REDACTED_AT_A_PRICED_COST: Final[list] = [
    pytest.param("?ref=main&path=src/app.py", f"?ref={REDACTED}&path={REDACTED}", id="ref-and-path"),
    pytest.param(
        "?source_branch=ac/x&target_branch=main&state=opened",
        f"?source_branch={REDACTED}&target_branch={REDACTED}&state={REDACTED}",
        id="the-merge-requests-query-this-gate-runs",
    ),
    pytest.param(
        "?per_page=100&page=2&scope=all",
        f"?per_page={REDACTED}&page={REDACTED}&scope={REDACTED}",
        id="the-pagination-this-repo-calls",
    ),
    pytest.param(
        "?sha=deadbeef&rev=HEAD~1&tag=v1.2.3",
        f"?sha={REDACTED}&rev={REDACTED}&tag={REDACTED}",
        id="git-object-vocabulary",
    ),
    pytest.param(f"?token={_T}#fragment", f"?token={REDACTED}#{REDACTED}", id="a-dropped-segment-is-marked-not-erased"),
    pytest.param(
        f"?ref=main,private_token={_T}", f"?ref={REDACTED},private_token={REDACTED}", id="a-pair-behind-a-pair"
    ),
    pytest.param(
        "rc=128: fatal: not a git repository",
        f"rc={REDACTED} fatal: not a git repository",
        id="the-shape-probe_cause-no-longer-emits",
    ),
    pytest.param("?%72%65%66=main", f"?%72%65%66={REDACTED}", id="an-escaped-name-is-still-a-pair"),
    pytest.param("?Per-Page=100", f"?Per-Page={REDACTED}", id="case-and-separator-folding-buys-nothing"),
]


# Both corpora, sampled coprime to every axis length so the stride cannot align with one.
_IDEMPOTENCE_CELLS: Final[list] = [pytest.param(cell.text, id=cell.label) for cell in (*_CORPUS, *_USERINFO_CORPUS)][
    ::97
]


_USERINFO_BOUNDARIES: Final[list] = [
    pytest.param("'", id="single-quote"),
    pytest.param('"', id="double-quote"),
    pytest.param("`", id="backtick"),
    pytest.param(" ", id="space"),
    pytest.param("\t", id="tab"),
    pytest.param("\xa0", id="nbsp"),
]


class TestARunBoundaryInsideAnAuthorityPublishesNoPrefix:
    """The run partition splits an authority BEFORE the userinfo rule ever sees it.

    Measured on the pre-fix scanner: `https://u:<token>'<word>@h.invalid/x` published
    the password at 52 characters with NO truncation involved, and a space did it
    too — so the input cap was never the cause. The half still attached to the scheme
    is the half that can be recovered; the half past the boundary is a bare word and
    stays the recorded R1/R2 residual.
    """

    @pytest.mark.parametrize("boundary", _USERINFO_BOUNDARIES)
    @pytest.mark.parametrize("offset", [4, 10, len(_T)], ids=["early", "mid", "whole-password"])
    def test_the_half_still_attached_to_the_scheme_is_redacted(self, boundary: str, offset: int) -> None:
        password = f"{_T[:offset]}{boundary}{_T[offset:]}word"
        redacted = redaction.redact_credentials(f"https://u:{password}@h.invalid/x")
        assert _T[:offset] not in redacted, f"the password's head survived into `{redacted}`"

    def test_the_measured_shape_carries_the_whole_password_out(self) -> None:
        text = f"https://u:{_T}'trailing-word@h.invalid/x"
        assert len(text) < redaction._MAX_SCANNED_CHARS, "the cap is not what this row is about"
        assert _T not in redaction.redact_credentials(text)

    def test_the_same_url_as_the_pushs_remote_carries_it_out_too(self) -> None:
        """The control that falsified truncation as the root: `bounded()` does not close it."""
        assert _T not in git_probes.bounded(f"https://u:{_T}'trailing-word@h.invalid/x")

    def test_the_control_a_terminated_authority_keeps_its_shape(self) -> None:
        assert redaction.redact_credentials(f"https://u:{_T}@h.invalid/x") == f"https://{REDACTED}@h.invalid/x"

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("see https://h.invalid/docs/x — it is fine", id="prose-around-a-url"),
            pytest.param("fatal: could not read from https://h.invalid/o/r.git", id="gits-own-prose"),
            pytest.param("ssh://git@h.invalid/o/r.git", id="a-login-name"),
            pytest.param("git://h.invalid/o/r.git", id="no-userinfo-at-all"),
        ],
    )
    def test_the_control_ordinary_text_carrying_an_authority_is_untouched(self, text: str) -> None:
        """A fix redacting everything after any `://` would satisfy every row above."""
        assert redaction.redact_credentials(text) == text

    def test_the_priced_cost_is_a_bare_host_that_ends_a_run(self) -> None:
        """Recorded rather than discovered: a host with no path is the one benign shape this costs."""
        assert redaction.redact_credentials("https://h.invalid") == f"https://{REDACTED}"


class TestTheTablesThatPriceTheTrade:
    @pytest.mark.parametrize(("text", "expected"), _MUST_REDACT_USERINFO)
    def test_a_userinfo_carries_no_credential_out(self, text: str, expected: str) -> None:
        assert redaction.redact_credentials(text) == expected

    @pytest.mark.parametrize("text", _MUST_KEEP)
    def test_a_look_alike_that_is_no_credential_stays_byte_identical(self, text: str) -> None:
        assert redaction.redact_credentials(text) == text

    @pytest.mark.parametrize(("text", "expected"), _REDACTED_AT_A_PRICED_COST)
    def test_the_priced_cost_is_what_the_table_says(self, text: str, expected: str) -> None:
        redacted = redaction.redact_credentials(text)
        assert redacted == expected
        assert redacted.startswith(text.split("=", 1)[0]), "the parameter name must survive"

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param("?ref_type=heads#installation", f"?ref_type={REDACTED}#{REDACTED}", id="a-url-fragment"),
            pytest.param(
                "rule=protected,contact,admin@example.com",
                f"rule={REDACTED},{REDACTED},{REDACTED}",
                id="a-comma-list-behind-a-pair",
            ),
        ],
    )
    def test_a_dropped_segment_is_marked(self, text: str, expected: str) -> None:
        """A tail dropped in silence reads as text that was never there — say it went."""
        assert redaction.redact_credentials(text) == expected

    @pytest.mark.parametrize("text", _IDEMPOTENCE_CELLS)
    def test_a_second_pass_changes_nothing(self, text: str) -> None:
        """The assembled refusal is scrubbed again after its parts were: it must be a no-op."""
        once = redaction.redact_credentials(text)
        assert redaction.redact_credentials(once) == once


class TestTheInputCap:
    """Past the cap the scanner drops text; a drop nobody can see is the defect.

    Measured at the shipped head: `redact_credentials("a" * 70000)` returned `''`,
    `bounded()` of the same returned `''`, and the refusal quoting it read `exit 128
    with no stderr` — a 70 KB answer rendered as no answer at all.
    """

    def test_an_operand_past_the_cap_is_truncated_at_a_run_boundary(self) -> None:
        text = f"quoted: {'a' * (64 * 1024)} ?token={_T}"
        redacted = redaction.redact_credentials(text)
        assert redacted.startswith("quoted: ")
        assert redacted.endswith(_TRUNCATED), "a whole run went missing with nothing saying so"
        assert _T not in redacted

    def test_a_cap_with_no_run_boundary_is_marked_not_erased(self) -> None:
        assert redaction.redact_credentials("a" * 70000) == _TRUNCATED

    def test_text_under_the_cap_is_marked_nowhere(self) -> None:
        """The control: the marker appears only where something really was dropped."""
        assert redaction.redact_credentials("short") == "short"

    def test_the_truncation_marker_survives_a_second_scrub(self) -> None:
        """The assembled refusal is scrubbed again, so a marker carrying `=` or `://` would eat itself."""
        assert redaction.redact_credentials(_TRUNCATED) == _TRUNCATED

    def test_a_scrub_of_an_over_cap_input_is_idempotent(self) -> None:
        once = redaction.redact_credentials(f"https://h.invalid/o/r.git?token={'a' * 70000}")
        assert redaction.redact_credentials(once) == once

    def test_clip_appends_an_ellipsis_only_past_the_limit(self) -> None:
        assert redaction.clip("abcdef", 6) == "abcdef"
        assert redaction.clip("abcdef", 3) == "abc…"
