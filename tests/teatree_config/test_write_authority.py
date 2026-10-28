"""Review phases may write private test databases, never the live config store."""

from pathlib import Path
from unittest.mock import patch

from teatree.config.write_authority import CONFIG_WRITE_PHASE_ENV, review_write_refusal


def test_review_refuses_only_canonical_database(tmp_path: Path) -> None:
    live = tmp_path / "live.sqlite3"
    scratch = tmp_path / "scratch.sqlite3"
    with patch("teatree.config.write_authority.canonical_config_db", return_value=live):
        assert "review or critic" in (review_write_refusal(live, env={CONFIG_WRITE_PHASE_ENV: "reviewing"}) or "")
        assert "review or critic" in (
            review_write_refusal(live, env={CONFIG_WRITE_PHASE_ENV: "critic_reviewing"}) or ""
        )
        for phase in ("review", "codex_reviewing", "codex_adversarial_reviewing", "e2e_review"):
            assert review_write_refusal(live, env={CONFIG_WRITE_PHASE_ENV: phase}) is not None
        assert review_write_refusal(scratch, env={CONFIG_WRITE_PHASE_ENV: "reviewing"}) is None
        assert review_write_refusal(":memory:", env={CONFIG_WRITE_PHASE_ENV: "reviewing"}) is None
        assert review_write_refusal(live, env={CONFIG_WRITE_PHASE_ENV: "coding"}) is None
