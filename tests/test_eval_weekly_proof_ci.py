"""The scheduled GitLab lane publishes and gates one exact-set proof."""

from typing import Any, cast

import yaml

from tests._ci_config import gitlab_ci_path


def _config() -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(gitlab_ci_path().read_text(encoding="utf-8")))


def test_weekly_shards_publish_distinct_summary_json_artifacts() -> None:
    config = _config()
    shared_script = "\n".join(config[".eval-suite"]["script"])
    assert "--summary-json" in shared_script
    assert "CI_NODE_INDEX" in shared_script
    assert "eval-summary-*.json" in config[".eval-suite"]["artifacts"]["paths"]
    assert "--judge" in shared_script
    assert "claude --version" in "\n".join(config[".eval-suite"]["before_script"])


def test_weekly_proof_is_blocking_and_consumes_all_shards() -> None:
    config = _config()
    proof = cast("dict[str, Any]", config["eval-weekly-proof"])
    assert "eval-weekly" in str(proof["needs"])
    assert "merge-summary-json" in "\n".join(proof["script"])
    assert "green-proof" in "\n".join(proof["script"])
    assert proof.get("allow_failure") is not True
    assert proof["variables"]["T3_OVERLAY_NAME"] == config[".eval-suite"]["variables"]["T3_OVERLAY_NAME"]
    assert proof["variables"]["XDG_DATA_HOME"] == config[".eval-suite"]["variables"]["XDG_DATA_HOME"]
    assert "migrate --no-input" in "\n".join(proof["script"])
    assert '--sha "$CI_COMMIT_SHA"' in "\n".join(proof["script"])


def test_selective_job_only_appears_for_eval_changes_and_empty_run_is_orange() -> None:
    config = _config()
    detect = cast("dict[str, Any]", config["eval-mr-detect"])
    selective = cast("dict[str, Any]", config["eval-mr-selective"])
    assert any("changes" in rule for rule in detect["rules"])
    assert any("changes" in rule for rule in selective["rules"])
    for job in (detect, selective):
        paths = job["rules"][0]["changes"]
        assert "vendor/teatree/scripts/eval/**/*" in paths
        assert "overlay/skills/**/*" in paths
        assert any(path.startswith("src/") and path.endswith("/overlay/overlay.py") for path in paths)
    assert 'exit "$EVAL_BLOCKED"' in "\n".join(config[".eval-suite"]["before_script"])
    assert "exit 75" in "\n".join(selective["script"])
    assert "coverage incomplete: $EVAL_DEFERRED deferred" in "\n".join(selective["script"])
    assert "--judge" in "\n".join(selective["script"])
    assert "--summary-json" in "\n".join(selective["script"])
    assert "eval-summary-*.json" in selective["artifacts"]["paths"]
    assert "script_failure" not in selective["retry"]["when"]
    assert "FAILED or UNVERIFIED scenarios" in "\n".join(selective["script"])


def test_policy_doc_explains_empty_selection() -> None:
    policy = gitlab_ci_path().parent / "AGENTS.md"
    assert "empty selective eval selection" in policy.read_text(encoding="utf-8").lower()
