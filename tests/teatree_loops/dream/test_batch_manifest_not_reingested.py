"""A dream batch's own manifest is never drift evidence for the next pass.

The coding session that implements a batch carries the manifest verbatim — in its
dispatch brief, its queue record, and any ``ticket context show`` output — so a pass
that distils it re-grounds each gap's Evidence under a fresh cluster key and re-offers
the gap the session is delivering.
"""

import json
import tempfile
from pathlib import Path

from django.test import TestCase

from teatree.core.models import ConsolidatedMemory
from teatree.dream_constants import DREAM_BATCH_MANIFEST_HEADER
from teatree.loop.scanners.backlog_sweep import BacklogSweepScanner
from teatree.loops.dream.engine import DistilledCluster, write_clusters
from teatree.loops.dream.replay import TranscriptMember, build_extract
from teatree.loops.dream.transcript_extract import high_signal_lines

UMBRELLA = "https://github.com/souliane/teatree/issues/2663"
RULE = "Before tightening a shared predicate, check every other caller of that predicate."
CITATION = "a liveness gate added for one call site silently changed behavior at a second call site"
GATE_BLOCK = "TEATREE GATE BLOCK: push refused, the affected lane is red"


def _manifest() -> str:
    ConsolidatedMemory.objects.create(
        cluster_key="gap-1",
        source_files=["feedback_gap.md"],
        is_binding=False,
        member_count=1,
        max_member_weight=90,
        rule=RULE,
        verified_citation=CITATION,
        durable_destination="skills/architecture-design/SKILL.md",
    )
    return BacklogSweepScanner(overlay_name="t3-teatree", dream_umbrella_url=UMBRELLA)._dream_gap_section(
        [{"gap_key": "gap-1", "title": "Workflow gap", "cluster_key": "gap-1"}]
    )


def _brief(manifest: str) -> str:
    return f"Work on ticket 1.\nTicket context:\n{manifest}\nWhen done, run the retro and make sure the envelope ships."


def _transcript(manifest: str) -> str:
    brief = _brief(manifest)
    return "\n".join(
        [
            json.dumps({"type": "queue-operation", "operation": "enqueue", "content": brief}),
            json.dumps({"type": "user", "message": {"role": "user", "content": brief}}),
            json.dumps({"type": "assistant", "message": {"role": "assistant", "content": GATE_BLOCK}}),
        ]
    )


class HighSignalLinesDropsTheManifestTestCase(TestCase):
    def test_a_line_carrying_the_manifest_never_reaches_the_extract(self) -> None:
        kept = high_signal_lines(_transcript(_manifest()))

        assert CITATION not in kept
        assert RULE not in kept

    def test_a_genuine_signal_line_in_the_same_transcript_survives(self) -> None:
        kept = high_signal_lines(_transcript(_manifest()))

        assert GATE_BLOCK in kept

    def test_the_same_lines_without_the_header_are_kept(self) -> None:
        unmarked = _manifest().replace(DREAM_BATCH_MANIFEST_HEADER, "")

        kept = high_signal_lines(_transcript(unmarked))

        assert CITATION in kept


class ManifestEvidenceDoesNotGroundTestCase(TestCase):
    def test_a_cluster_citing_the_manifest_evidence_is_rejected(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        session = tmp / "session.jsonl"
        session.write_text(_transcript(_manifest()))
        remint = DistilledCluster(
            cluster_key="remint",
            rule=RULE,
            source_files=[str(session)],
            is_binding=False,
            verified_citation=CITATION,
            durable_destination="skills/architecture-design/SKILL.md",
        )

        extract = build_extract([TranscriptMember(path=session, kind="main")])
        outcome = write_clusters([remint], extract, dry_run=False)

        assert (outcome.written, outcome.rejected) == (0, 1)
        assert not ConsolidatedMemory.objects.filter(cluster_key="remint").exists()


class ManifestRendersTheSharedHeaderTestCase(TestCase):
    def test_the_manifest_places_the_header_after_the_directive_the_extract_filters_on(self) -> None:
        manifest = _manifest()
        directive, marked_block = manifest.split("\n", 1)

        assert "DREAM GAPS:" in directive
        assert marked_block.startswith(f"{DREAM_BATCH_MANIFEST_HEADER}\n")
