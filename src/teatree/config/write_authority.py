"""Keep review and critic verification away from the live config database."""

import os
from collections.abc import Mapping
from pathlib import Path

from teatree.config.cold_reader import canonical_config_db
from teatree.config.phase_names import REVIEW_PHASES, normalize_phase

CONFIG_WRITE_PHASE_ENV = "T3_CONFIG_WRITE_PHASE"


def _review_phase(phase: str) -> bool:
    normalized = normalize_phase(phase.replace("-", "_"))
    return normalized in REVIEW_PHASES or (
        normalized != "requesting_review" and normalized.endswith(("_review", "_reviewing"))
    )


def review_write_refusal(db_path: str | Path, *, env: Mapping[str, str] = os.environ) -> str | None:
    if not _review_phase(env.get(CONFIG_WRITE_PHASE_ENV, "")):
        return None
    if str(db_path) in {"", ":memory:"} or str(db_path).startswith("file:"):
        return None
    canonical = canonical_config_db(env=env)
    if Path(db_path).expanduser().resolve() != canonical.expanduser().resolve():
        return None
    return "a review or critic session may not write the live control database; use a throwaway test database"
