"""Which model a run is recorded as served by, and the window it ran in (souliane/teatree#4874)."""

from teatree.agents.runner_usage import UsageObservation, _attempt_usage, _billed_model
from tests.teatree_agents._sdk_fake import result_message

#: A Sonnet run as the CLI reports it: the Haiku auxiliary call sorts FIRST, the served model dwarfs it.
_FALLBACK_RUN_USAGE = {
    "claude-haiku-4-5-20251001": {
        "inputTokens": 1_200,
        "outputTokens": 90,
        "cacheReadInputTokens": 0,
        "cacheCreationInputTokens": 0,
        "contextWindow": 200_000,
    },
    "claude-sonnet-5": {
        "inputTokens": 256,
        "outputTokens": 93_581,
        "cacheReadInputTokens": 27_568_835,
        "cacheCreationInputTokens": 313_014,
        "contextWindow": 1_000_000,
    },
}


class TestTheServedModelIsTheDominantOne:
    def test_the_haiku_auxiliary_listed_first_is_not_the_served_model(self) -> None:
        assert _billed_model(_FALLBACK_RUN_USAGE) == "claude-sonnet-5"

    def test_a_single_entry_is_the_served_model_whatever_it_reports(self) -> None:
        assert _billed_model({"claude-opus-5": {}}) == "claude-opus-5"

    def test_no_entries_is_no_served_model(self) -> None:
        assert _billed_model(None) == ""
        assert _billed_model({}) == ""


class TestTheConversationSizeAndWindowAreRecorded:
    def test_the_window_is_the_served_models_and_the_size_is_the_streams(self) -> None:
        usage = _attempt_usage(
            result_message(model_usage=_FALLBACK_RUN_USAGE, num_turns=162),
            UsageObservation(context_tokens=313_270, model_fell_back=True),
        )

        assert usage.model == "claude-sonnet-5"
        assert usage.context_window_tokens == 1_000_000
        assert usage.context_tokens == 313_270
        assert usage.model_fell_back

    def test_a_stream_that_died_before_its_result_still_keeps_what_it_measured(self) -> None:
        usage = _attempt_usage(None, UsageObservation(context_tokens=313_270, model_fell_back=True))

        assert usage.context_tokens == 313_270
        assert usage.model_fell_back

    def test_a_served_model_that_reported_no_window_leaves_it_unmeasured(self) -> None:
        usage = _attempt_usage(result_message(model_usage={"claude-opus-5": {}}))

        assert usage.context_window_tokens is None
        assert usage.context_tokens is None
