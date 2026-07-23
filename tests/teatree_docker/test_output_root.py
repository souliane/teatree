"""Which compose services pin the agent output root (souliane/teatree#3641)."""

from pathlib import Path

from teatree.docker.output_root import services_missing_output_root


def _compose(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "docker-compose.yml"
    path.write_text(body, encoding="utf-8")
    return path


def test_service_without_tmpdir_is_reported(tmp_path: Path) -> None:
    compose = _compose(tmp_path, "services:\n  worker:\n    image: t3\n")
    assert services_missing_output_root(compose) == ["worker"]


def test_mapping_form_tmpdir_counts_as_pinned(tmp_path: Path) -> None:
    compose = _compose(tmp_path, "services:\n  worker:\n    environment:\n      TMPDIR: /var/tmp\n")
    assert services_missing_output_root(compose) == []


def test_list_form_tmpdir_counts_as_pinned(tmp_path: Path) -> None:
    # Compose accepts both shapes; a `KEY=value` list entry pins it just as well.
    compose = _compose(tmp_path, "services:\n  worker:\n    environment:\n      - TMPDIR=/var/tmp\n")
    assert services_missing_output_root(compose) == []


def test_env_file_alone_does_not_count(tmp_path: Path) -> None:
    # An `env_file` is read by the service main process, but `docker exec` resolves
    # its env from the container config — which only an inline `environment` sets.
    compose = _compose(tmp_path, "services:\n  worker:\n    env_file:\n      - teatree.env\n")
    assert services_missing_output_root(compose) == ["worker"]


def test_only_the_unpinned_services_are_named(tmp_path: Path) -> None:
    compose = _compose(
        tmp_path,
        "services:\n  pinned:\n    environment:\n      TMPDIR: /var/tmp\n  unpinned:\n    image: t3\n",
    )
    assert services_missing_output_root(compose) == ["unpinned"]


def test_unreadable_compose_reports_nothing(tmp_path: Path) -> None:
    assert services_missing_output_root(tmp_path / "absent.yml") == []


def test_malformed_compose_reports_nothing(tmp_path: Path) -> None:
    # A parse failure is not evidence of a missing pin; the caller is advisory.
    assert services_missing_output_root(_compose(tmp_path, "services: [oops\n")) == []


def test_a_non_mapping_document_reports_nothing(tmp_path: Path) -> None:
    # Valid YAML that parses to a list (not a compose mapping) — nothing to report.
    assert services_missing_output_root(_compose(tmp_path, "- a\n- b\n")) == []


def test_a_non_mapping_services_key_reports_nothing(tmp_path: Path) -> None:
    # `services` present but not a mapping (a list) — no service names to inspect.
    assert services_missing_output_root(_compose(tmp_path, "services:\n  - worker\n")) == []
