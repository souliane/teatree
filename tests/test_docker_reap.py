"""Tests for teatree.docker.reap — compose-project container + image reaping.

The docker subprocess is the only mocked boundary: a fake ``run_allowed_to_fail``
records the commands and serves canned ``docker ps`` / ``docker images`` /
``docker inspect`` output. The label scoping
(``com.docker.compose.project=<project>``) is what keeps base images and the
main-clone deps image safe — compose only labels artifacts it built for that
project, so they never appear under a removed worktree's project.
"""

from datetime import UTC, datetime
from subprocess import CompletedProcess

import pytest

from teatree.docker.reap import (
    ReapResult,
    _parse_docker_timestamp,
    list_compose_projects,
    project_last_activity,
    reap_compose_project,
    reap_orphan_compose_projects,
    reap_stale_compose_projects,
    stale_compose_projects,
)
from teatree.utils.run import TimeoutExpired

_LABEL = "com.docker.compose.project"
_MISSING_DOCKER = "docker"


def _scoped_project(cmd: list[str]) -> str:
    for arg in cmd:
        if arg.startswith(f"label={_LABEL}="):
            return arg.removeprefix(f"label={_LABEL}=")
    return ""


def _is_enumeration(cmd: list[str]) -> bool:
    return f"label={_LABEL}" in cmd and not _scoped_project(cmd)


class _FakeDocker:
    """Canned docker daemon: serves per-project containers/images and records removals.

    ``containers`` / ``images`` map a scoped project to the ids its filter
    returns; ``enumerated`` is the project-label list returned by the unscoped
    enumeration calls (``list_compose_projects``). Removal commands are recorded
    rather than executed.
    """

    def __init__(
        self,
        *,
        containers: dict[str, list[str]] | None = None,
        images: dict[str, list[str]] | None = None,
        enumerated: list[str] | None = None,
        inspect: dict[str, str] | None = None,
    ) -> None:
        self.containers = containers or {}
        self.images = images or {}
        self.enumerated = enumerated or []
        self.inspect = inspect or {}
        self.removed_containers: list[str] = []
        self.removed_images: list[str] = []
        self.calls: list[list[str]] = []

    def __call__(self, cmd, *, expected_codes=None, timeout=None, **_kwargs) -> CompletedProcess:
        del expected_codes, timeout
        cmd = list(cmd)
        self.calls.append(cmd)
        return CompletedProcess(cmd, 0, self._stdout(cmd), "")

    def _remove(self, cmd: list[str]) -> str:
        sink = self.removed_containers if cmd[1] == "rm" else self.removed_images
        sink.extend(cmd[3:])
        return "\n".join(cmd[3:]) + "\n"

    def _stdout(self, cmd: list[str]) -> str:
        if cmd[:3] in (["docker", "rm", "-f"], ["docker", "rmi", "-f"]):
            return self._remove(cmd)
        if cmd[:2] == ["docker", "inspect"]:
            ids = cmd[4:]  # ["docker", "inspect", "--format", <fmt>, *ids]
            return "\n".join(self.inspect.get(cid, "") for cid in ids) + "\n"
        if _is_enumeration(cmd):
            return "\n".join(self.enumerated) + "\n"
        if cmd[:2] == ["docker", "ps"]:
            return "\n".join(self.containers.get(_scoped_project(cmd), [])) + "\n"
        if cmd[:2] == ["docker", "images"]:
            return "\n".join(self.images.get(_scoped_project(cmd), [])) + "\n"
        return ""


def _patch(monkeypatch: pytest.MonkeyPatch, fake: object) -> None:
    monkeypatch.setattr("teatree.docker.reap.run_allowed_to_fail", fake)


class TestReapResult:
    def test_is_noop_only_when_nothing_removed(self) -> None:
        assert ReapResult(project="p").is_noop
        assert not ReapResult(project="p", images_removed=1).is_noop

    def test_str_names_the_project_and_counts(self) -> None:
        text = str(ReapResult(project="backend-wt9", containers_removed=2, images_removed=1))
        assert "backend-wt9" in text
        assert "2 container(s)" in text
        assert "1 image(s)" in text


class TestReapComposeProject:
    def test_removes_containers_and_images_for_the_project(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeDocker(
            containers={"backend-wt99": ["backend-wt99-web-1", "backend-wt99-db-1"]},
            images={"backend-wt99": ["sha256:img1", "sha256:img2"]},
        )
        _patch(monkeypatch, fake)

        result = reap_compose_project("backend-wt99")

        assert fake.removed_containers == ["backend-wt99-web-1", "backend-wt99-db-1"]
        assert fake.removed_images == ["sha256:img1", "sha256:img2"]
        assert result.containers_removed == 2
        assert result.images_removed == 2
        assert result.project == "backend-wt99"

    def test_no_artifacts_is_a_clean_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeDocker()
        _patch(monkeypatch, fake)

        result = reap_compose_project("gone-wt7")

        assert result.is_noop
        assert fake.removed_containers == []
        assert fake.removed_images == []

    def test_empty_project_name_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeDocker()
        _patch(monkeypatch, fake)

        result = reap_compose_project("")

        assert result.is_noop
        assert fake.calls == []

    def test_filters_by_compose_project_label(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeDocker()
        _patch(monkeypatch, fake)

        reap_compose_project("backend-wt99")

        list_calls = [c for c in fake.calls if c[:2] in (["docker", "ps"], ["docker", "images"])]
        assert list_calls, "expected a ps + images listing"
        for call in list_calls:
            assert f"label={_LABEL}=backend-wt99" in call

    def test_missing_docker_binary_is_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def raise_missing(cmd, **_kwargs):
            raise FileNotFoundError(_MISSING_DOCKER)

        _patch(monkeypatch, raise_missing)

        assert reap_compose_project("backend-wt99").is_noop

    def test_timeout_is_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def raise_timeout(cmd, **_kwargs):
            raise TimeoutExpired(cmd, 30)

        _patch(monkeypatch, raise_timeout)

        assert reap_compose_project("backend-wt99").is_noop

    def test_denied_docker_is_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A sandbox that denies the docker binary (PermissionError) is a clean skip."""

        def raise_denied(cmd, **_kwargs):
            raise PermissionError(_MISSING_DOCKER)

        _patch(monkeypatch, raise_denied)

        assert reap_compose_project("backend-wt99").is_noop


def _is_image_label_format(cmd: list[str]) -> bool:
    """Whether *cmd* is a ``docker images ... --format '{{.Label ...}}'`` call.

    ``.Label`` is invalid on docker's image formatter context (#2361) — the
    daemon raises ``can't evaluate field Label in type *formatter.imageContext``.
    The fixtures below use this to refuse the bad template the way real docker
    does, so a regression that reaches for ``.Label`` on images turns red.
    """
    return cmd[:2] == ["docker", "images"] and any(".Label " in arg for arg in cmd)


class TestListComposeProjects:
    def test_aggregates_project_labels_across_containers_and_images(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Project names come from container labels directly and image labels via inspect."""

        def fake(cmd, *, expected_codes=None, timeout=None, **_kwargs):
            del expected_codes, timeout
            cmd = list(cmd)
            if cmd[:2] == ["docker", "ps"]:
                return CompletedProcess(cmd, 0, "backend-wt1\nbackend-wt2\n\n", "")
            if cmd[:2] == ["docker", "images"]:
                return CompletedProcess(cmd, 0, "sha256:img-a\nsha256:img-b\n", "")
            if cmd[:3] == ["docker", "image", "inspect"]:
                return CompletedProcess(cmd, 0, "backend-wt2\nfrontend-wt3\n", "")
            return CompletedProcess(cmd, 0, "", "")

        _patch(monkeypatch, fake)

        assert list_compose_projects() == {"backend-wt1", "backend-wt2", "frontend-wt3"}

    def test_image_enumeration_never_uses_the_invalid_label_field(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """#2361 regression: ``docker images`` must enumerate by ``.ID`` + inspect, never ``.Label``.

        The fake daemon mirrors real docker — it returns a non-zero
        template-parsing error for any ``docker images --format '{{.Label ...}}'``
        call. If the production code asked for ``.Label`` on images, that arm
        would fire and the recorded ``docker images`` call would carry the
        forbidden field.
        """
        calls: list[list[str]] = []

        def fake(cmd, *, expected_codes=None, timeout=None, **_kwargs):
            del expected_codes, timeout
            cmd = list(cmd)
            calls.append(cmd)
            if _is_image_label_format(cmd):
                return CompletedProcess(cmd, 1, "", "template parsing error: can't evaluate field Label")
            if cmd[:2] == ["docker", "images"]:
                return CompletedProcess(cmd, 0, "sha256:img-a\n", "")
            if cmd[:3] == ["docker", "image", "inspect"]:
                return CompletedProcess(cmd, 0, "orphan-wt9\n", "")
            return CompletedProcess(cmd, 0, "", "")

        _patch(monkeypatch, fake)

        assert list_compose_projects() == {"orphan-wt9"}

        image_calls = [c for c in calls if c[:2] == ["docker", "images"]]
        assert image_calls, "expected a docker images enumeration"
        for call in image_calls:
            assert not any(".Label " in arg for arg in call), f"invalid .Label field on image formatter: {call}"
            assert "{{.ID}}" in call, f"image enumeration must select ids: {call}"
        inspect_calls = [c for c in calls if c[:3] == ["docker", "image", "inspect"]]
        assert inspect_calls, "expected a docker image inspect to resolve the project label"
        for call in inspect_calls:
            assert any("Config.Labels" in arg for arg in call), f"inspect must read .Config.Labels map: {call}"
            assert "sha256:img-a" in call, "inspect must target the enumerated image ids"

    def test_no_images_skips_the_inspect_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty image-id list must not fire an argument-less ``docker image inspect``."""
        calls: list[list[str]] = []

        def fake(cmd, *, expected_codes=None, timeout=None, **_kwargs):
            del expected_codes, timeout
            cmd = list(cmd)
            calls.append(cmd)
            if cmd[:2] == ["docker", "ps"]:
                return CompletedProcess(cmd, 0, "backend-wt1\n", "")
            return CompletedProcess(cmd, 0, "", "")

        _patch(monkeypatch, fake)

        assert list_compose_projects() == {"backend-wt1"}
        assert not any(c[:3] == ["docker", "image", "inspect"] for c in calls)

    def test_ignores_blank_lines(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake(cmd, *, expected_codes=None, timeout=None, **_kwargs):
            del expected_codes, timeout
            cmd = list(cmd)
            if cmd[:2] == ["docker", "ps"]:
                return CompletedProcess(cmd, 0, "backend-wt1\n\n   \n", "")
            return CompletedProcess(cmd, 0, "", "")

        _patch(monkeypatch, fake)

        assert list_compose_projects() == {"backend-wt1"}

    def test_missing_docker_returns_empty_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def raise_missing(cmd, **_kwargs):
            raise FileNotFoundError(_MISSING_DOCKER)

        _patch(monkeypatch, raise_missing)

        assert list_compose_projects() == set()

    def test_daemon_error_returncode_yields_empty_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake(cmd, *, expected_codes=None, timeout=None, **_kwargs):
            del expected_codes, timeout
            return CompletedProcess(list(cmd), 1, "", "Cannot connect to the Docker daemon")

        _patch(monkeypatch, fake)

        assert list_compose_projects() == set()


class TestReapOrphanComposeProjects:
    def test_reaps_only_projects_absent_from_live_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeDocker(
            containers={"orphan-wt9": ["orphan-wt9-web-1"]},
            images={"orphan-wt9": ["sha256:orphanimg"]},
            enumerated=["live-wt1", "orphan-wt9"],
        )
        _patch(monkeypatch, fake)

        results = reap_orphan_compose_projects(live_projects={"live-wt1"})

        assert fake.removed_containers == ["orphan-wt9-web-1"]
        assert fake.removed_images == ["sha256:orphanimg"]
        assert [r.project for r in results] == ["orphan-wt9"]

    def test_no_orphans_when_every_project_is_live(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeDocker(enumerated=["live-wt1"])
        _patch(monkeypatch, fake)

        results = reap_orphan_compose_projects(live_projects={"live-wt1"})

        assert results == []
        assert fake.removed_containers == []
        assert fake.removed_images == []


# ── Stale-stack reaping (#2207) ──────────────────────────────────────────────

_NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)
_OLD = "2026-06-10T01:00:00.123456789Z"  # 11h before _NOW — past any sane threshold
_FRESH = "2026-06-10T11:45:00Z"  # 15m before _NOW — must never be reaped
_ZERO = "0001-01-01T00:00:00Z"  # docker's "never happened" value


class TestParseDockerTimestamp:
    def test_nanosecond_precision_is_parsed(self) -> None:
        parsed = _parse_docker_timestamp(_OLD)
        assert parsed == datetime(2026, 6, 10, 1, 0, 0, 123456, tzinfo=UTC)

    def test_zero_value_and_garbage_yield_none(self) -> None:
        assert _parse_docker_timestamp(_ZERO) is None
        assert _parse_docker_timestamp("") is None
        assert _parse_docker_timestamp("not-a-time") is None


class TestProjectLastActivity:
    def test_newest_lifecycle_event_across_containers_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeDocker(
            containers={"stack-a": ["c1", "c2"]},
            inspect={
                "c1": f"{_OLD}|{_OLD}|{_ZERO}",
                "c2": f"{_OLD}|{_FRESH}|{_ZERO}",
            },
        )
        _patch(monkeypatch, fake)

        assert project_last_activity("stack-a") == datetime(2026, 6, 10, 11, 45, 0, tzinfo=UTC)

    def test_no_containers_means_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch(monkeypatch, _FakeDocker())

        assert project_last_activity("ghost") is None


class TestStaleComposeProjects:
    def test_old_unowned_stack_is_selected_and_reaped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeDocker(
            containers={"abandoned-test": ["c1"]},
            enumerated=["abandoned-test"],
            inspect={"c1": f"{_OLD}|{_OLD}|{_OLD}"},
        )
        _patch(monkeypatch, fake)

        selected = stale_compose_projects(set(), min_age_minutes=240, now=_NOW)
        assert selected == ["abandoned-test"]

        results = reap_stale_compose_projects(set(), min_age_minutes=240, now=_NOW)
        assert [r.project for r in results] == ["abandoned-test"]
        assert fake.removed_containers == ["c1"]

    def test_fresh_unowned_stack_is_kept(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A parallel session's just-started manual stack must never be torn down."""
        fake = _FakeDocker(
            containers={"parallel-test": ["c1"]},
            enumerated=["parallel-test"],
            inspect={"c1": f"{_OLD}|{_FRESH}|{_ZERO}"},
        )
        _patch(monkeypatch, fake)

        assert stale_compose_projects(set(), min_age_minutes=240, now=_NOW) == []
        assert reap_stale_compose_projects(set(), min_age_minutes=240, now=_NOW) == []
        assert fake.removed_containers == []

    def test_unknown_age_fails_safe_to_keep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeDocker(
            containers={"mystery": ["c1"]},
            enumerated=["mystery"],
            inspect={"c1": f"{_ZERO}|{_ZERO}|{_ZERO}"},
        )
        _patch(monkeypatch, fake)

        assert stale_compose_projects(set(), min_age_minutes=240, now=_NOW) == []
        assert fake.removed_containers == []

    def test_live_project_is_never_selected_even_when_old(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeDocker(
            containers={"backend-wt5": ["c1"]},
            enumerated=["backend-wt5"],
            inspect={"c1": f"{_OLD}|{_OLD}|{_ZERO}"},
        )
        _patch(monkeypatch, fake)

        assert stale_compose_projects({"backend-wt5"}, min_age_minutes=240, now=_NOW) == []
        assert fake.removed_containers == []

    def test_dry_selection_removes_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeDocker(
            containers={"abandoned-test": ["c1"]},
            enumerated=["abandoned-test"],
            inspect={"c1": f"{_OLD}|{_OLD}|{_OLD}"},
        )
        _patch(monkeypatch, fake)

        assert stale_compose_projects(set(), min_age_minutes=240, now=_NOW) == ["abandoned-test"]
        assert fake.removed_containers == []
        assert fake.removed_images == []
