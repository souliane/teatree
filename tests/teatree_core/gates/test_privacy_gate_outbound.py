"""Tests for the egress-chokepoint wrapper ``scan_outbound_text`` (#1295).

``scan_outbound_text`` derives public-ness from the bash gates' visibility axis
(``_target_is_public`` → ``is_public_destination``) — NOT a per-overlay
``public_repos`` list — and delegates to the pure :func:`scan_for_publication`
with the overlay's redact/quote rules plus the always-on built-in quote anchors.

The MANDATORY anti-inertness test (``test_real_config_blocks_quote_anchor_leak_to_public_repo``)
mocks NONE of the gate's rules or public-ness resolution — only the network
visibility probe — and proves a leaking body to ``souliane/teatree`` is BLOCKED
against the real resolved config (the gap the prior version shipped: empty
``public_repos`` made the gate inert).
"""

import json
import sqlite3
from collections.abc import Callable, Sequence
from types import SimpleNamespace

import pytest
from django.core.exceptions import ImproperlyConfigured

from teatree.core.gates import privacy_gate
from teatree.core.gates.privacy_gate import format_refusal, scan_outbound_text
from teatree.hooks import _repo_visibility, publish_destination
from tests._send_gate import TEST_TERM_REGISTRY

REAL_PUBLIC = "souliane/teatree"
REDACT = "SECRETCORP"


@pytest.fixture
def inject_rules(monkeypatch: pytest.MonkeyPatch, configured_banned_term_registry: None):
    def _set(*, public: bool, redact: Sequence[str] = (), block: Sequence[str] = ()) -> None:
        monkeypatch.setattr(privacy_gate, "_target_is_public", lambda _repo, _forge: public)
        monkeypatch.setattr(privacy_gate, "overlay_privacy_rules", lambda _name="": (list(redact), list(block)))

    return _set


def test_real_config_blocks_quote_anchor_leak_to_public_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    # No mock of the overlay rules or public-ness — only the network probe — so
    # the block comes from the REAL resolved config: souliane/teatree is public
    # via the visibility axis and the built-in quote anchors always fire.
    monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
    result = scan_outbound_text(
        text="Posting what the user said verbatim to the public PR.",
        target_repo=REAL_PUBLIC,
        forge="github",
    )
    assert result.refused
    assert result.is_public


def test_real_config_public_repo_scanned_even_when_probe_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    # Probe returns None (tool absent) → the visibility axis fails CLOSED to
    # public, so the leak is still blocked — detection failure never disables it.
    monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: None)
    monkeypatch.setattr(_repo_visibility, "_read_visibility_cache", lambda _slug: None)
    result = scan_outbound_text(
        text="The user said this verbatim and it must not leak.",
        target_repo=REAL_PUBLIC,
        forge="github",
    )
    assert result.refused


def test_blocks_overlay_redact_term_on_public_repo(inject_rules) -> None:
    inject_rules(public=True, redact=[REDACT])
    result = scan_outbound_text(text=f"This review note touches {REDACT} internals.", target_repo=REAL_PUBLIC)
    assert result.refused
    assert any(match.pattern_name == f"redact:{REDACT}" for match in result.matches)


def test_blocks_builtin_quote_anchor_with_no_overlay_terms(inject_rules) -> None:
    inject_rules(public=True)
    result = scan_outbound_text(text="The user said this verbatim in chat.", target_repo=REAL_PUBLIC)
    assert result.refused


def test_clean_body_passes(inject_rules) -> None:
    inject_rules(public=True, redact=[REDACT])
    result = scan_outbound_text(text="A perfectly ordinary review note.", target_repo=REAL_PUBLIC)
    assert not result.refused


def test_registry_allow_entry_is_exempt_only_where_it_occurs_exactly(inject_rules) -> None:
    inject_rules(public=True)
    allowed = TEST_TERM_REGISTRY["allow"][0]

    assert not scan_outbound_text(text=f"Ship {allowed} today.", target_repo=REAL_PUBLIC).refused
    assert scan_outbound_text(text=f"Ship {allowed}-bot today.", target_repo=REAL_PUBLIC).refused


def test_block_patterns_still_see_an_allow_listed_identifier(inject_rules) -> None:
    allowed = TEST_TERM_REGISTRY["allow"][0]
    inject_rules(public=True, block=[allowed])

    result = scan_outbound_text(text=f"Ship {allowed} today.", target_repo=REAL_PUBLIC)

    assert result.refused
    assert [match.pattern_name for match in result.matches] == [f"block:{allowed}"]


def test_private_target_passes_same_leaking_content(inject_rules) -> None:
    inject_rules(public=False, redact=[REDACT])
    result = scan_outbound_text(text=f"This touches {REDACT} verbatim user said.", target_repo="acme/private")
    assert not result.refused
    assert result.is_public is False


class TestFullForgeUrlTarget:
    """A URL-shaped ``target_repo`` classifies as the SLUG it names, both directions.

    The programmatic publish paths (``t3 ... ticket comment``/``create-sub`` via
    ``route_forge_write``, the on-behalf ``reply_transport``) name their destination
    by full issue/work-item URL, not by bare slug. Nothing here mocks
    ``_target_is_public``: the classifier under test is the real
    ``is_public_destination``, driven off a seeded config, so an internal URL is a
    clean pass AND a genuinely-public URL still refuses the same body.
    """

    LEAKING = f"This note names {REDACT} internals."

    @pytest.fixture
    def internal_config(self, tmp_path, monkeypatch: pytest.MonkeyPatch):
        db = tmp_path / "config.sqlite3"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE teatree_config_setting ("
            "id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', ?, ?)",
            [
                ("private_repos", json.dumps(["gitlab.example/internalcorp"])),
            ],
        )
        conn.commit()
        conn.close()
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        monkeypatch.setattr(privacy_gate, "overlay_privacy_rules", lambda _name="": ([REDACT], []))
        return db

    def test_internal_work_item_url_passes_flagged_body(self, internal_config) -> None:
        result = scan_outbound_text(
            text=self.LEAKING,
            target_repo="https://gitlab.example/internalcorp/group/app/-/work_items/14",
            forge="gitlab",
        )
        assert result.is_public is False
        assert not result.refused

    def test_public_issue_url_still_refuses_the_same_body(
        self, internal_config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
        result = scan_outbound_text(
            text=self.LEAKING,
            target_repo=f"https://github.com/{REAL_PUBLIC}/issues/5",
            forge="github",
        )
        assert result.is_public
        assert result.refused

    def test_self_hosted_private_remote_exempts_generated_pr_body(
        self, internal_config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: None)
        conn = sqlite3.connect(str(internal_config))
        conn.execute(
            "UPDATE teatree_config_setting SET value = ? WHERE key = 'private_repos'",
            (json.dumps(["gitlab.corp.example/group/repo"]),),
        )
        conn.commit()
        conn.close()

        assert (
            privacy_gate.refusal_for_pr_generic_findings(
                title="fix",
                description="Contact person@firm.tld",  # privacy-scan:allow synthetic detector fixture
                target_repo="git@gitlab.corp.example:group/repo.git",
                forge="",
            )
            is None
        )  # privacy-scan:allow synthetic fixture


def test_classification_error_fails_closed_to_scanning(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_args: object, **_kwargs: object) -> bool:
        raise RuntimeError

    # ``_target_is_public`` re-imports ``is_public_destination`` per call, so
    # patching it here makes the classification raise → the gate must fail
    # CLOSED (treat as public, scan), so the built-in anchor still blocks.
    monkeypatch.setattr(publish_destination, "is_public_destination", _boom)
    monkeypatch.setattr(privacy_gate, "overlay_privacy_rules", lambda _name="": ([], []))
    result = scan_outbound_text(text="User mandate (verbatim leak here.", target_repo=REAL_PUBLIC, forge="github")
    assert result.refused


def test_overlay_redact_rules_empty_when_no_overlay_is_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    # An EMPTY registry is the only case with genuinely no overlay rules to lose:
    # the built-in quote anchors stay the floor. An AMBIGUOUS registry is NOT this
    # case — see tests/teatree_core/gates/test_privacy_gate_multi_overlay.py.
    def _raise(*_args: object, **_kwargs: object) -> object:
        raise ImproperlyConfigured

    monkeypatch.setattr(privacy_gate, "get_overlay", _raise)
    assert privacy_gate.overlay_privacy_rules() == ([], [])


def test_overlay_redact_rules_read_from_resolved_overlay_config(monkeypatch: pytest.MonkeyPatch) -> None:
    config = SimpleNamespace(privacy_redact_terms=[REDACT], privacy_block_patterns=["custom-pattern"])
    monkeypatch.setattr(privacy_gate, "get_overlay", lambda *_a, **_k: SimpleNamespace(config=config))
    assert privacy_gate.overlay_privacy_rules() == ([REDACT], ["custom-pattern"])


# --- F2.2: overlay-rule RESOLUTION FAILURE fails the public publish CLOSED + loud ---

_RESOLUTION_FAILURE_MSG = "overlay registry wedged"


def test_overlay_rules_none_on_genuine_resolution_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    # An overlay IS present but reading it raises an UNEXPECTED error (not the
    # ImproperlyConfigured that means "no single overlay"). The overlay's own
    # redact/block rules would silently vanish, so the resolver returns None.
    def _boom(*_a: object, **_k: object) -> object:
        raise RuntimeError(_RESOLUTION_FAILURE_MSG)

    monkeypatch.setattr(privacy_gate, "get_overlay", _boom)
    assert privacy_gate.overlay_privacy_rules() is None


def test_overlay_rules_none_when_config_fields_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    # get_overlay() resolves, but reading the privacy fields off the config raises
    # → a present-but-unreadable overlay is a resolution failure → None (fail closed).
    class _AngryConfig:
        @property
        def privacy_redact_terms(self) -> list[str]:
            raise RuntimeError(_RESOLUTION_FAILURE_MSG)

        @property
        def privacy_block_patterns(self) -> list[str]:
            return []

    monkeypatch.setattr(privacy_gate, "get_overlay", lambda *_a, **_k: SimpleNamespace(config=_AngryConfig()))
    assert privacy_gate.overlay_privacy_rules() is None


def test_public_publish_refused_when_overlay_rules_unresolvable(monkeypatch: pytest.MonkeyPatch) -> None:
    # The confidentiality boundary: a PUBLIC target whose overlay rules cannot be
    # resolved is REFUSED (fail closed + loud), NOT scanned with only the built-ins.
    monkeypatch.setattr(privacy_gate, "_target_is_public", lambda _repo, _forge: True)
    monkeypatch.setattr(privacy_gate, "overlay_privacy_rules", lambda _name="": None)
    result = scan_outbound_text(text="A perfectly ordinary note.", target_repo=REAL_PUBLIC, forge="github")
    assert result.refused
    assert result.is_public
    assert any(match.pattern_name == "overlay-rules-unresolvable" for match in result.matches)


def test_private_target_not_refused_even_when_rules_unresolvable(monkeypatch: pytest.MonkeyPatch) -> None:
    # A provably-PRIVATE target is a clean pass regardless of rule resolution —
    # the fail-closed refusal is scoped to public targets only.
    monkeypatch.setattr(privacy_gate, "_target_is_public", lambda _repo, _forge: False)
    monkeypatch.setattr(privacy_gate, "overlay_privacy_rules", lambda _name="": None)
    result = scan_outbound_text(text="A note.", target_repo="acme/private", forge="github")
    assert not result.refused
    assert result.is_public is False


@pytest.mark.parametrize(
    ("category", "body"),
    [
        ("email", "Contact person@firm.tld"),  # privacy-scan:allow synthetic fixture
        ("home_path", "Read /home/employee/report"),  # privacy-scan:allow synthetic fixture
        ("private_ip", "Connect to 10.24.8.9"),  # privacy-scan:allow synthetic fixture
        ("api_key", "Token glpat-XXXXXXXXXXXXXXXX"),  # privacy-scan:allow synthetic fixture
        ("internal_hostname", "Host build.internal.corp"),  # privacy-scan:allow synthetic fixture
    ],
)
def test_generic_detector_blocks_public_forge_write(
    inject_rules: Callable[..., None], category: str, body: str
) -> None:
    inject_rules(public=True)

    result = scan_outbound_text(text=body, target_repo=REAL_PUBLIC)

    assert result.refused
    assert f"generic:{category}" in [match.pattern_name for match in result.matches]


def test_generic_refusal_redacts_token_and_remote_credentials() -> None:
    synthetic_api_key = "glpat-XXXXXXXXXXXXXXXX"  # privacy-scan:allow synthetic detector fixture
    result = privacy_gate.PrivacyGateResult(
        target_repo="https://u:private-token@github.com/souliane/teatree.git",  # privacy-scan:allow synthetic fixture
        is_public=True,
        matches=(privacy_gate.PrivacyMatch("generic:api_key", synthetic_api_key, 0),),
    )  # privacy-scan:allow synthetic fixture

    refusal = format_refusal(result)
    assert "glpat-XXXXXXXXXXXXXXXX" not in refusal  # privacy-scan:allow synthetic fixture
    assert "private-token" not in refusal
    assert "github.com/souliane/teatree" in refusal
    assert "[redacted]" in refusal


def test_generic_detector_keeps_exact_allow_and_refuses_extended_address(
    inject_rules: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pins scan_outbound_text's masked-text handoff to _generic_matches.
    monkeypatch.setenv(
        "TEATREE_TERM_REGISTRY",
        json.dumps(
            {
                "leak": ["placeholder-leak"],
                "prose_collider": [],
                "tone": [],
                "allow": ["person@firm.tld", "firm"],  # privacy-scan:allow synthetic fixture
            }
        ),
    )  # privacy-scan:allow synthetic fixture
    inject_rules(public=True)

    assert not scan_outbound_text(
        text="person@firm.tld",  # privacy-scan:allow synthetic fixture
        target_repo=REAL_PUBLIC,
    ).refused  # privacy-scan:allow synthetic fixture
    assert scan_outbound_text(
        text="person@firm.tld.biz",  # privacy-scan:allow synthetic fixture
        target_repo=REAL_PUBLIC,
    ).refused  # privacy-scan:allow synthetic fixture
    assert scan_outbound_text(
        text="other@firm.tld",  # privacy-scan:allow synthetic fixture
        target_repo=REAL_PUBLIC,
    ).refused  # privacy-scan:allow synthetic fixture


def test_generic_detector_exempts_public_ci_path_only_on_forge_write(inject_rules: Callable[..., None]) -> None:
    # Pins _generic_matches' forge_write=True call into generic_findings.
    inject_rules(public=True)
    body = "/home/runner/work/project"  # privacy-scan:allow synthetic fixture

    assert not scan_outbound_text(text=body, target_repo=REAL_PUBLIC).refused
    assert any(hit.category == "home_path" for hit in privacy_gate.generic_findings(body))


def test_pr_generic_refusal_is_independent_of_unreadable_term_rules(
    inject_rules: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    inject_rules(public=True)
    monkeypatch.setattr(privacy_gate, "overlay_privacy_rules", lambda _name="": None)

    refusal = privacy_gate.refusal_for_pr_generic_findings(
        title="fix",
        description="Contact person@firm.tld",  # privacy-scan:allow synthetic fixture
        target_repo=REAL_PUBLIC,
        forge="github",
    )

    assert refusal is not None
    assert "generic:email" in refusal


def test_pr_generic_refusal_honors_an_exact_allow_next_to_a_term(
    inject_rules: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    inject_rules(public=True)
    monkeypatch.setenv(
        "TEATREE_TERM_REGISTRY",
        json.dumps(
            {
                "leak": ["placeholder-leak"],
                "prose_collider": [],
                "tone": [],
                "allow": ["person@firm.tld"],  # privacy-scan:allow synthetic fixture
            }
        ),
    )  # privacy-scan:allow synthetic fixture

    assert (
        privacy_gate.refusal_for_pr_generic_findings(
            title="fix",
            description="person@firm.tld placeholder-leak",  # privacy-scan:allow synthetic fixture
            target_repo=REAL_PUBLIC,
            forge="github",
        )
        is None
    )


def test_generic_detector_ignores_push_scanner_marker_on_forge_write(inject_rules: Callable[..., None]) -> None:
    inject_rules(public=True)

    result = scan_outbound_text(
        text="Contact person@firm.tld # privacy-scan:allow", target_repo=REAL_PUBLIC
    )  # privacy-scan:allow synthetic fixture

    assert result.refused


def test_generic_detector_skips_private_forge_target(inject_rules: Callable[..., None]) -> None:
    inject_rules(public=False)

    result = scan_outbound_text(
        text="Contact person@firm.tld",  # privacy-scan:allow synthetic fixture
        target_repo="acme/private",
    )  # privacy-scan:allow synthetic fixture

    assert not result.refused
