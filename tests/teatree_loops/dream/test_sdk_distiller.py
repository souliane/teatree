"""Tests for the real LLM distiller seam — extract → clusters, no live LLM (#2723)."""

import json
import threading
from pathlib import Path
from typing import Self
from unittest.mock import patch

import claude_agent_sdk
import pytest
from django.test import SimpleTestCase

from teatree.loops.dream import sdk_distiller
from teatree.loops.dream.engine import ConsolidationExtract, DistillEmptyReason, WeightedSnippet
from teatree.loops.dream.sdk_distiller import deterministic_cluster_key
from tests.teatree_agents._sdk_fake import FakeSdkClient, assistant_text


def _extract_with_one_snippet() -> ConsolidationExtract:
    return ConsolidationExtract(
        snippets=(WeightedSnippet(path=Path("/feedback_x.md"), kind="memory", weight=9, text="BINDING: x"),),
        truncated=False,
    )


class SdkDistillerParseTestCase(SimpleTestCase):
    def test_parses_clusters_from_json(self) -> None:
        payload = (
            '[{"cluster_key":"k1","rule":"do x","source_files":["/feedback_x.md"],'
            '"is_binding":true,"verified_citation":"the mistake","durable_destination":"d.md"}]'
        )
        with patch.object(sdk_distiller, "_run_distiller_turn", return_value=payload):
            clusters = sdk_distiller.sdk_distiller(_extract_with_one_snippet())
        assert len(clusters) == 1
        # #2723: the cluster_key is a deterministic sha256 over the member set, NOT
        # the LLM-supplied "k1" slug.
        assert clusters[0].cluster_key == deterministic_cluster_key(["/feedback_x.md"])
        assert clusters[0].cluster_key != "k1"
        assert clusters[0].is_binding is True
        assert clusters[0].source_files == ["/feedback_x.md"]

    def test_parses_json_embedded_in_prose(self) -> None:
        payload = (
            "Here is the result:\n"
            '[{"cluster_key":"k1","rule":"do x","source_files":["/f.md"],'
            '"is_binding":false,"verified_citation":"m","durable_destination":""}]\n'
            "Done."
        )
        with patch.object(sdk_distiller, "_run_distiller_turn", return_value=payload):
            clusters = sdk_distiller.sdk_distiller(_extract_with_one_snippet())
        assert len(clusters) == 1

    def test_parses_json_with_bracketed_prose_around_the_array(self) -> None:
        # #2847 RED: the model wraps its array in bracket-heavy prose (a markdown-ish
        # ref and a trailing marker). The greedy first-"[" .. last-"]" span captured the
        # prose brackets, json.loads raised, and the batch silently yielded 0. The
        # balanced-bracket scan must skip the prose "[...]" spans and return the real array.
        payload = (
            "Here are the clusters [per your guidance #2663]: "
            '[{"cluster_key":"k1","rule":"do x","source_files":["/feedback_x.md"],'
            '"is_binding":true,"verified_citation":"x","durable_destination":"d.md"}]'
            " — done [end]"
        )
        with patch.object(sdk_distiller, "_run_distiller_turn", return_value=payload):
            clusters = sdk_distiller.sdk_distiller(_extract_with_one_snippet())
        assert len(clusters) == 1
        assert clusters[0].source_files == ["/feedback_x.md"]

    def test_parses_json_from_a_fenced_code_block(self) -> None:
        # The model wraps its array in a ```json fence; the direct decode fails on the
        # backticks, so the fenced-block tier must extract and decode the inner array.
        payload = (
            "```json\n"
            '[{"cluster_key":"k1","rule":"do x","source_files":["/feedback_x.md"],'
            '"is_binding":false,"verified_citation":"x","durable_destination":""}]\n'
            "```"
        )
        with patch.object(sdk_distiller, "_run_distiller_turn", return_value=payload):
            clusters = sdk_distiller.sdk_distiller(_extract_with_one_snippet())
        assert len(clusters) == 1

    def test_balanced_scan_skips_string_brackets_and_a_bad_prose_span(self) -> None:
        # A leading prose "[noise]" span must be skipped, and a JSON string value carrying
        # an escaped quote and bracket characters must NOT skew the bracket depth.
        payload = (
            "prose [noise] "
            '[{"cluster_key":"k1","rule":"match \\"x\\" in [a-z]+",'
            '"source_files":["/feedback_x.md"],"is_binding":false,'
            '"verified_citation":"q","durable_destination":""}]'
        )
        with patch.object(sdk_distiller, "_run_distiller_turn", return_value=payload):
            clusters = sdk_distiller.sdk_distiller(_extract_with_one_snippet())
        assert len(clusters) == 1
        assert clusters[0].source_files == ["/feedback_x.md"]

    def test_fenced_non_array_falls_through_to_the_balanced_scan(self) -> None:
        # A ```json fence wrapping a JSON object (not an array) is not the result; the
        # scan must fall through to the real array that follows in prose.
        payload = (
            '```json\n{"not": "an array"}\n```\n'
            '[{"rule":"r","source_files":["/feedback_x.md"],'
            '"is_binding":false,"verified_citation":"m","durable_destination":""}]'
        )
        with patch.object(sdk_distiller, "_run_distiller_turn", return_value=payload):
            clusters = sdk_distiller.sdk_distiller(_extract_with_one_snippet())
        assert len(clusters) == 1
        assert clusters[0].source_files == ["/feedback_x.md"]

    def test_json_object_not_array_yields_no_clusters(self) -> None:
        # A decodable JSON object (not a list) is not an array — it yields no clusters.
        with patch.object(sdk_distiller, "_run_distiller_turn", return_value='{"not": "an array"}'):
            clusters = sdk_distiller.sdk_distiller(_extract_with_one_snippet())
        assert clusters == []

    def test_malformed_json_yields_no_clusters(self) -> None:
        with patch.object(sdk_distiller, "_run_distiller_turn", return_value="not json at all"):
            clusters = sdk_distiller.sdk_distiller(_extract_with_one_snippet())
        assert clusters == []

    def test_skips_entries_missing_required_keys(self) -> None:
        # cluster_key is NO LONGER required from the LLM (it is derived). A missing
        # rule/source_files still drops the entry; a complete one is kept.
        payload = (
            '[{"is_binding":false},'
            '{"rule":"r","source_files":["/f.md"],'
            '"is_binding":false,"verified_citation":"m","durable_destination":""}]'
        )
        with patch.object(sdk_distiller, "_run_distiller_turn", return_value=payload):
            clusters = sdk_distiller.sdk_distiller(_extract_with_one_snippet())
        assert len(clusters) == 1
        assert clusters[0].source_files == ["/f.md"]

    def test_cluster_key_is_deterministic_over_member_set(self) -> None:
        # #2723: two payloads with the SAME member set but DIFFERENT LLM slugs derive
        # the SAME cluster_key (and member order does not matter).
        def _payload(slug: str, files: str) -> str:
            return (
                f'[{{"cluster_key":"{slug}","rule":"r","source_files":{files},'
                '"is_binding":false,"verified_citation":"m","durable_destination":""}]'
            )

        with patch.object(sdk_distiller, "_run_distiller_turn", return_value=_payload("slug-one", '["/b.md","/a.md"]')):
            ca = sdk_distiller.sdk_distiller(_extract_with_one_snippet())
        with patch.object(sdk_distiller, "_run_distiller_turn", return_value=_payload("slug-two", '["/a.md","/b.md"]')):
            cb = sdk_distiller.sdk_distiller(_extract_with_one_snippet())
        assert ca[0].cluster_key == cb[0].cluster_key
        # And it is a sha256 hex digest, matching the model docstring.
        assert len(ca[0].cluster_key) == 64

    def test_blank_and_whitespace_paths_are_dropped_before_hashing(self) -> None:
        # The normalization that anchors idempotency: blanks dropped, dupes collapsed,
        # order ignored — so "  /a.md ", "/a.md", "" hash to the same key as ["/a.md"].
        key_noisy = deterministic_cluster_key(["  /a.md ", "/a.md", "", "   "])
        key_clean = deterministic_cluster_key(["/a.md"])
        assert key_noisy == key_clean

    def test_malformed_json_array_decode_error_yields_no_clusters(self) -> None:
        # A bracketed-but-INVALID payload reaches json.loads and raises JSONDecodeError;
        # the parse swallows it to [] rather than crashing the pass.
        payload = '[{"rule": "r", not valid json]'
        with patch.object(sdk_distiller, "_run_distiller_turn", return_value=payload):
            clusters = sdk_distiller.sdk_distiller(_extract_with_one_snippet())
        assert clusters == []

    def test_non_object_and_bad_source_files_entries_are_dropped(self) -> None:
        # A non-Mapping element and a complete element whose source_files is not a list
        # are both dropped; only the well-formed element survives.
        payload = (
            '["a bare string, not an object",'
            '{"rule":"r","source_files":"not-a-list",'
            '"is_binding":false,"verified_citation":"m","durable_destination":""},'
            '{"rule":"r","source_files":["/f.md"],'
            '"is_binding":false,"verified_citation":"m","durable_destination":""}]'
        )
        with patch.object(sdk_distiller, "_run_distiller_turn", return_value=payload):
            clusters = sdk_distiller.sdk_distiller(_extract_with_one_snippet())
        assert len(clusters) == 1
        assert clusters[0].source_files == ["/f.md"]

    def test_missing_claude_binary_raises(self) -> None:
        # The guard: with no claude on PATH the real turn raises rather than faking a
        # success, so the pass is marked attempted-not-succeeded.
        with (
            patch("shutil.which", return_value=None),
            pytest.raises(RuntimeError, match="claude is not installed"),
        ):
            sdk_distiller._run_distiller_turn(_extract_with_one_snippet())

    def test_live_turn_renders_snippets_and_parses_the_sdk_reply(self) -> None:
        # The real _run_distiller_turn -> _collect_turn path with the SDK client faked:
        # the prompt carries the rendered snippet, and the assistant's JSON reply is
        # parsed into a cluster (deterministic key over the cited member).
        reply = json.dumps(
            [
                {
                    "cluster_key": "ignored-slug",
                    "rule": "do x",
                    "source_files": ["/feedback_x.md"],
                    "is_binding": True,
                    "verified_citation": "x",
                    "durable_destination": "d.md",
                }
            ]
        )

        def _make_client(*, options: object = None, **_: object) -> FakeSdkClient:
            return FakeSdkClient([assistant_text(reply)])

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch.object(claude_agent_sdk, "ClaudeSDKClient", _make_client),
        ):
            clusters = sdk_distiller.sdk_distiller(_extract_with_one_snippet())
        assert len(clusters) == 1
        assert clusters[0].cluster_key == deterministic_cluster_key(["/feedback_x.md"])
        assert "BINDING: x" in FakeSdkClient.last_prompt

    def test_sdk_turn_failure_raises(self) -> None:
        with (
            patch.object(sdk_distiller, "_run_distiller_turn", side_effect=RuntimeError("sdk boom")),
            pytest.raises(RuntimeError),
        ):
            sdk_distiller.sdk_distiller(_extract_with_one_snippet())

    def test_empty_extract_short_circuits_without_sdk_call(self) -> None:
        empty = ConsolidationExtract(snippets=(), truncated=False)
        with patch.object(sdk_distiller, "_run_distiller_turn") as turn:
            clusters = sdk_distiller.sdk_distiller(empty)
        turn.assert_not_called()
        assert clusters == []


class SdkDistillReasonTestCase(SimpleTestCase):
    """The 0-cluster path is diagnosable: sdk_distill signals WHY it produced 0 (#2847)."""

    def test_empty_raw_is_classified(self) -> None:
        with patch.object(sdk_distiller, "_run_distiller_turn", return_value="   "):
            result = sdk_distiller.sdk_distill(_extract_with_one_snippet())
        assert result.clusters == []
        assert result.empty_reason is DistillEmptyReason.EMPTY_RAW

    def test_unparsable_raw_is_classified(self) -> None:
        with patch.object(sdk_distiller, "_run_distiller_turn", return_value="not json at all"):
            result = sdk_distiller.sdk_distill(_extract_with_one_snippet())
        assert result.clusters == []
        assert result.empty_reason is DistillEmptyReason.UNPARSABLE

    def test_genuine_empty_array_is_healthy(self) -> None:
        with patch.object(sdk_distiller, "_run_distiller_turn", return_value="[]"):
            result = sdk_distiller.sdk_distill(_extract_with_one_snippet())
        assert result.clusters == []
        assert result.empty_reason is DistillEmptyReason.NOTHING_TO_CONSOLIDATE

    def test_array_with_all_entries_dropped_is_classified(self) -> None:
        with patch.object(sdk_distiller, "_run_distiller_turn", return_value='[{"is_binding":false}]'):
            result = sdk_distiller.sdk_distill(_extract_with_one_snippet())
        assert result.clusters == []
        assert result.empty_reason is DistillEmptyReason.ALL_ENTRIES_DROPPED

    def test_productive_result_carries_no_reason(self) -> None:
        payload = (
            '[{"rule":"r","source_files":["/feedback_x.md"],'
            '"is_binding":false,"verified_citation":"m","durable_destination":""}]'
        )
        with patch.object(sdk_distiller, "_run_distiller_turn", return_value=payload):
            result = sdk_distiller.sdk_distill(_extract_with_one_snippet())
        assert len(result.clusters) == 1
        assert result.empty_reason is None

    def test_empty_extract_is_nothing_to_consolidate_without_sdk_call(self) -> None:
        empty = ConsolidationExtract(snippets=(), truncated=False)
        with patch.object(sdk_distiller, "_run_distiller_turn") as turn:
            result = sdk_distiller.sdk_distill(empty)
        turn.assert_not_called()
        assert result.clusters == []
        assert result.empty_reason is DistillEmptyReason.NOTHING_TO_CONSOLIDATE


class _HangOnConnectClient:
    """A ``ClaudeSDKClient`` stand-in whose connect (``__aenter__``) never returns.

    Models a ``claude`` subprocess that stalls during spawn/handshake — the region
    the prior watchdog (which wrapped only the response drain) did NOT cover, so a
    real stall there hung the dream pass forever.
    """

    def __init__(self, *, options: object = None, **_: object) -> None:
        self._options = options

    async def __aenter__(self) -> Self:
        import asyncio  # noqa: PLC0415

        await asyncio.sleep(30)  # connect stalls; only the turn watchdog can bound it
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def query(self, prompt: str) -> None:  # pragma: no cover - connect hangs first
        return None

    async def receive_response(self) -> object:  # pragma: no cover - connect hangs first
        return
        yield  # unreachable


class SdkDistillerWatchdogTestCase(SimpleTestCase):
    def test_turn_is_time_bounded_when_sdk_connect_hangs(self) -> None:
        # Anti-vacuous regression pin for the silent-hang bug: a stalled ``claude``
        # CONNECT must raise TimeoutError within the watchdog, never hang the dream
        # pass forever. RED on the pre-fix code whose watchdog wrapped only the
        # response drain (leaving connect/query unbounded); GREEN once the watchdog
        # bounds the WHOLE turn. Run on a thread so a regression hangs the THREAD,
        # not the suite, and is observed as a still-alive thread.
        captured: dict[str, BaseException | None] = {}

        def _run() -> None:
            try:
                sdk_distiller._run_distiller_turn(_extract_with_one_snippet())
                captured["exc"] = None
            except BaseException as exc:  # noqa: BLE001 - record whatever the turn raised
                captured["exc"] = exc

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch.object(sdk_distiller, "_DISTILL_WATCHDOG_SECONDS", 0.5),
            patch.object(claude_agent_sdk, "ClaudeSDKClient", _HangOnConnectClient),
        ):
            thread = threading.Thread(target=_run, daemon=True)
            thread.start()
            thread.join(timeout=8)

        assert not thread.is_alive(), (
            "distiller SDK turn was NOT time-bounded: a stalled claude connect hangs the dream pass forever"
        )
        assert isinstance(captured.get("exc"), TimeoutError), (
            f"expected the watchdog to raise TimeoutError on a stalled turn, got {captured.get('exc')!r}"
        )
