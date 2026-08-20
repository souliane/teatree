"""The push/pull classifier: deny-by-default, and questions always interrupt."""

import pytest

from teatree.core.modelkit.dm_channel_policy import PUSH_SIGNALS, DmChannel, classify, signal_prefixes
from teatree.core.modelkit.notify_policy import NotifyAudience

#: The status signals measured on the live BotPing ledger over a 7-day window. Each is
#: pull material, and each must stay pull — the whole point of the gate.
MEASURED_STATUS_SIGNALS = (
    "pr_sweep_aged_skip:souliane/teatree#4533:20684",
    "watchdog:red:35983065672a79bb:0:20260819",
    "reconciliation:review_dispatch_saturation:2026-08-19",
    "pr_sweep_red_set:souliane/teatree:20260819",
    "merge-announce:souliane/teatree#4512:e6ea2782",
    "scanner_error:pr_sweep:20260819",
    "max-turns-truncation:t3-4305-coding",
    "usage_window_recovered:20260819",
    "pr-sweep-flag:souliane/teatree#4499:ci_red",
    "t3-4305-coding-heartbeat-1",
)


@pytest.mark.parametrize("key", MEASURED_STATUS_SIGNALS)
@pytest.mark.parametrize("audience", [NotifyAudience.OWNER_ESCALATION, NotifyAudience.OWNER_DELIVERY])
def test_measured_status_signals_are_pulled(key: str, audience: NotifyAudience) -> None:
    assert classify(audience=audience, idempotency_key=key) is DmChannel.PULL


@pytest.mark.parametrize(
    "key",
    ["a-brand-new-alarm:whatever", "no-colon-at-all", "", ":", "watchdog", "watchdogging:red"],
)
def test_an_unregistered_signal_defaults_to_pull(key: str) -> None:
    """The deny-by-default proof: a new alarm cannot reach the DM by being written."""
    assert classify(audience=NotifyAudience.OWNER_ESCALATION, idempotency_key=key) is DmChannel.PULL


@pytest.mark.parametrize(
    "key",
    ["mirror-deferred-question:abc", "resurface-deferred-question:abc", "a-brand-new-alarm:whatever", ""],
)
def test_a_question_always_pushes_whatever_its_signal(key: str) -> None:
    """The positive pair: over-blocking would silence the one class the DM is for."""
    assert classify(audience=NotifyAudience.OWNER_QUESTION, idempotency_key=key) is DmChannel.PUSH


def test_a_colleague_action_receipt_still_pushes() -> None:
    assert classify(audience=NotifyAudience.COLLEAGUE_ACTION, idempotency_key="on_behalf_post:x") is DmChannel.PUSH


def test_internal_is_pull() -> None:
    assert classify(audience=NotifyAudience.INTERNAL, idempotency_key="waiting_digest:abc") is DmChannel.PULL


@pytest.mark.parametrize(
    "key",
    [
        "watchdog:compose-up-failed:20260819",
        "watchdog:doctor-unreachable:20260819",
        "watchdog:doctor-no-verdict:20260819",
    ],
)
def test_a_registered_outage_page_pushes(key: str) -> None:
    assert classify(audience=NotifyAudience.OWNER_DELIVERY, idempotency_key=key) is DmChannel.PUSH


def test_push_signals_membership_is_pinned_exactly() -> None:
    """Adding a slug without touching this test is the conversation the gate exists to force."""
    assert (
        frozenset(
            {
                "watchdog:compose-up-failed",
                "watchdog:doctor-unreachable",
                "watchdog:doctor-no-verdict",
            }
        )
        == PUSH_SIGNALS
    )


def test_signal_prefixes_walks_every_colon_boundary() -> None:
    assert signal_prefixes("watchdog:red:abc") == ("watchdog", "watchdog:red", "watchdog:red:abc")
    assert signal_prefixes("solo") == ("solo",)
    assert signal_prefixes("") == ()
