"""Cross-key config consistency validator (souliane/teatree#3688).

Pure-logic unit tests for the write-time (agent_harness, agent_harness_provider)
consistency check — the config-layer mirror of the dispatch-time
``harness_registry.assert_provider_valid_for_harness`` constraint. No DB, no env:
the validator is a pure function over the resulting pair.
"""

from teatree.config.cross_key_consistency import check_harness_provider_pair, validate_cross_key_write


class TestHarnessProviderPair:
    def test_consistent_claude_sdk_pairs_pass(self) -> None:
        assert check_harness_provider_pair("claude_sdk", "subscription_oauth") is None
        assert check_harness_provider_pair("claude_sdk", "api_key") is None

    def test_consistent_pydantic_ai_pairs_pass(self) -> None:
        assert check_harness_provider_pair("pydantic_ai", "openai_compatible") is None
        assert check_harness_provider_pair("pydantic_ai", "anthropic_api") is None

    def test_inconsistent_pair_is_rejected_naming_both_sides(self) -> None:
        reason = check_harness_provider_pair("claude_sdk", "openai_compatible")
        assert reason is not None
        assert "openai_compatible" in reason
        assert "claude_sdk" in reason

    def test_api_key_under_pydantic_ai_is_rejected(self) -> None:
        assert check_harness_provider_pair("pydantic_ai", "api_key") is not None

    def test_retired_orca_router_byok_alias_under_claude_sdk_is_rejected(self) -> None:
        # The production trigger: the retired 'orca_router_byok' value aliases
        # forward to openai_compatible, which is valid only under pydantic_ai.
        assert check_harness_provider_pair("claude_sdk", "orca_router_byok") is not None

    def test_retired_orca_router_byok_alias_under_pydantic_ai_passes(self) -> None:
        assert check_harness_provider_pair("pydantic_ai", "orca_router_byok") is None

    def test_absent_or_blank_provider_always_passes(self) -> None:
        assert check_harness_provider_pair("claude_sdk", None) is None
        assert check_harness_provider_pair("pydantic_ai", "") is None
        assert check_harness_provider_pair("claude_sdk", "   ") is None

    def test_unset_harness_resolves_to_the_claude_sdk_default(self) -> None:
        assert check_harness_provider_pair(None, "openai_compatible") is not None
        assert check_harness_provider_pair(None, "subscription_oauth") is None

    def test_unparseable_provider_is_left_to_the_registry_parser(self) -> None:
        assert check_harness_provider_pair("claude_sdk", "not_a_provider") is None

    def test_overlay_registered_harness_is_unconstrained(self) -> None:
        # A non-built-in (overlay-registered) harness carries no closed-enum
        # constraint here; the open registry enforces it at dispatch.
        assert check_harness_provider_pair("vertex_enterprise", "openai_compatible") is None


class TestValidateCrossKeyWrite:
    def test_unrelated_key_is_a_no_op(self) -> None:
        assert validate_cross_key_write("mode", "auto", lambda _key: None) is None

    def test_setting_provider_reads_the_current_harness(self) -> None:
        resolve = {"agent_harness": "claude_sdk"}.get
        assert validate_cross_key_write("agent_harness_provider", "openai_compatible", resolve) is not None

    def test_setting_harness_reads_the_current_provider(self) -> None:
        resolve = {"agent_harness_provider": "openai_compatible"}.get
        assert validate_cross_key_write("agent_harness", "claude_sdk", resolve) is not None

    def test_setting_harness_to_a_matching_transport_passes(self) -> None:
        resolve = {"agent_harness_provider": "openai_compatible"}.get
        assert validate_cross_key_write("agent_harness", "pydantic_ai", resolve) is None

    def test_provider_with_no_stored_harness_uses_the_default(self) -> None:
        assert validate_cross_key_write("agent_harness_provider", "openai_compatible", lambda _key: None) is not None

    def test_provider_valid_under_default_harness_passes(self) -> None:
        assert validate_cross_key_write("agent_harness_provider", "subscription_oauth", lambda _key: None) is None
