"""Shared fixtures for teatree script tests."""

import datetime as dt
import importlib.util
import json
import os
import tempfile
import time
import types
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from teatree.config.host_projection import SILENCE_ADVISORY_ENV, reset_advisory_memo
from teatree.core.factory import external_outcomes
from teatree.core.management.commands._e2e_specs_checkout import release_process_locks
from teatree.core.worktree.branch_classification import reset_forge_probe_cache, reset_single_branch_cache
from teatree.loop.scanners.my_prs_ci import reset_ci_memo
from teatree.utils import ram_scope
from teatree.utils.work_tree import reset_cwd_cache
from tests._db_template import build_or_reuse_template, restore_from_template
from tests._machine_probe import pinned_ram_headroom
from tests._speak_thread_sentinel import SpeakThreadSentinel
from tests._thread_db_sentinel import ThreadDbHandleSentinel

# Keep a stale shell DJANGO_SETTINGS_MODULE out of any SUBPROCESS a test spawns. The
# suite's own settings are pinned by ``--ds`` in pyproject's addopts, because this pop
# lands after pytest-django has already resolved the module and so never stopped an
# ambient value winning here (#3996).
os.environ.pop("DJANGO_SETTINGS_MODULE", None)
# Pin T3_OVERLAY_NAME to the in-repo overlay so tests stay deterministic even
# when extra overlays are editable-installed for dogfooding (see #120). Tests
# that exercise overlay resolution override via monkeypatch.setenv/delenv.
os.environ["T3_OVERLAY_NAME"] = "t3-teatree"

# Guard against import-time side effects in script modules that call _init.init()
# at module import. Route HOME/T3_WORKSPACE_DIR to a disposable temp sandbox.
_IMPORT_SANDBOX = tempfile.TemporaryDirectory(prefix="teatree-tests-import-")
_IMPORT_HOME = Path(_IMPORT_SANDBOX.name) / "home"
_IMPORT_WORKSPACE = _IMPORT_HOME / "workspace"
_IMPORT_WORKSPACE.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("HOME", str(_IMPORT_HOME))
os.environ.setdefault("T3_WORKSPACE_DIR", str(_IMPORT_WORKSPACE))


# Config-source controls. Stripping these would defeat reproducing the CI
# default-branch condition locally: the CI image's git defaults to ``master``,
# so a fixture that assumes ``main`` exits 128. ``GIT_CONFIG_NOSYSTEM=1`` forces
# git's compiled-in ``master`` default on a dev box whose system/global config
# bakes in ``main`` (souliane/teatree#2359). These do not carry the parent
# repo's index/worktree the way the hook vars below do, so they are safe to keep.
_GIT_CONFIG_SOURCE_VARS = frozenset(
    {"GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"},
)


def _strip_git_hook_env() -> None:
    """Strip GIT_* env vars inherited from pre-commit hooks.

    When pytest runs as a prek/pre-commit hook via ``git commit -a``, git sets
    ``GIT_INDEX_FILE`` to ``.git/index.lock``. Hook subprocesses inherit this,
    so any git operation in a test (e.g. ``git init`` in a temp dir) corrupts
    the parent repo's index. Stripping the hook ``GIT_*`` vars at session start
    prevents this. See https://github.com/j178/prek/issues/1786.

    Config-source controls (``GIT_CONFIG_NOSYSTEM`` and friends) are preserved
    so the CI default-branch condition is reproducible locally.
    """
    for var in list(os.environ):
        if var.startswith("GIT_") and var not in _GIT_CONFIG_SOURCE_VARS:
            del os.environ[var]


_strip_git_hook_env()


def load_script(name: str) -> types.ModuleType:
    """Dynamically load a teatree script as a module for testing."""
    p = Path(__file__).resolve().parent.parent / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{name}_mod", p)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_ok(stdout: str = "", stderr: str = "", returncode: int = 0) -> MagicMock:
    """Create a MagicMock simulating a successful subprocess.run result."""
    return MagicMock(stdout=stdout, stderr=stderr, returncode=returncode)


@pytest.fixture
def pg_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set standard Postgres env vars for testing."""
    monkeypatch.setenv("POSTGRES_HOST", "localhost")
    monkeypatch.setenv("POSTGRES_USER", "testuser")
    monkeypatch.setenv("POSTGRES_PASSWORD", "testpass")


#: A fixed instant, deliberately mid-cycle, for every billing-cycle-scoped assertion.
#: A cycle-scoped reader filters ``started_at >= cycle_start(localdate())``, so a test
#: that stamps its attempt from the wall clock and then lets the reader re-read the
#: clock silently loses the attempt whenever the local date rolls onto a cycle start
#: between the two reads — the #3996 shuffle-lane red, which hit whichever test was on
#: the clock at that second. ``test_pinned_clock_sits_well_inside_its_cycle`` keeps this
#: value away from either boundary.
CYCLE_MIDPOINT = dt.datetime(2026, 6, 10, 12, 0, tzinfo=dt.UTC)


@pytest.fixture
def pinned_clock() -> Iterator[dt.datetime]:
    """Pin ``timezone.now`` (and therefore ``localdate``) to :data:`CYCLE_MIDPOINT`."""
    with patch("django.utils.timezone.now", return_value=CYCLE_MIDPOINT):
        yield CYCLE_MIDPOINT


@pytest.fixture(autouse=True)
def _clear_backend_caches() -> Iterator[None]:
    """Clear caches and block real token resolution so tests never call gpg/pass.

    ``backend_factory.reset_backend_caches`` (not the partial
    ``loader.reset_backend_caches``) is used because only the former
    also clears ``backend_factory._messaging_cache`` — otherwise a test
    that builds a real messaging backend leaks it under the empty-overlay
    key and a later ``notify_user`` (no explicit backend) reuses it,
    reaching a real ``pass`` subprocess. Patching the canonical
    ``teatree.utils.secrets.read_pass`` neutralises the whole posting-credential
    path: the #117 send-path reader ``send_proxy.read_posting_credential`` (every
    backend constructor routes through it) reaches ``read_pass`` via the module,
    not a bound import, so the single source-module patch is enough.
    """
    from unittest.mock import patch  # noqa: PLC0415

    import teatree.utils.secrets as _secrets_mod  # noqa: PLC0415
    from teatree.core.backend_factory import reset_backend_caches  # noqa: PLC0415
    from teatree.core.overlay_loader import reset_overlay_cache  # noqa: PLC0415

    def _no_pass(_key: str) -> str:
        return ""

    reset_backend_caches()
    reset_overlay_cache()
    with patch.object(_secrets_mod, "read_pass", _no_pass):
        yield
    reset_backend_caches()
    reset_overlay_cache()


@pytest.fixture(autouse=True)
def _silence_host_projection_advisory() -> Iterator[None]:
    """Keep the once-per-process host-projection advisory off every test's stderr.

    No projection is published under test, so the cold readers correctly fall back and
    warn — but that warning is a process-global write emitted on the FIRST call in each
    xdist worker, so it lands in whichever test happened to run first and is read as
    trailing output (`json.loads(result.output)` then fails on "Extra data"). That makes
    the victim a function of shard and shuffle seed rather than of any change.

    Silenced through the env seam the function itself reads, never `mock.patch`:
    `teatree.config.cold_db` binds `warn_once` directly, so a patched module attribute
    would not reach the live caller. `tests/teatree_config/test_host_projection.py`
    unsets it to cover the advisory in both directions.
    """
    reset_advisory_memo()
    with patch.dict(os.environ, {SILENCE_ADVISORY_ENV: "1"}):
        yield
    reset_advisory_memo()


@pytest.fixture(autouse=True)
def _reset_declaration_caches() -> Iterator[None]:
    """Drop the process-memoised repo declarations so one test's config never answers another's."""
    reset_single_branch_cache()
    reset_ci_memo()
    reset_forge_probe_cache()
    yield
    reset_single_branch_cache()
    reset_ci_memo()
    reset_forge_probe_cache()


@pytest.fixture(autouse=True)
def _pin_machine_memory_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the admission governor off THIS box's free memory.

    ``read_machine_signal`` reads the cgroup-aware probe and the governor brakes at/under
    ``RAM_BRAKE_FLOOR_GB``, so an unpinned suite reds tests that never mention memory
    whenever the host is under pressure. A test that cares about the reading patches this
    same seam in its own body, which lands after this fixture and therefore wins.
    """
    monkeypatch.setattr(ram_scope, "read_ram_headroom", pinned_ram_headroom)


@pytest.fixture(autouse=True)
def _inert_ambient_process_table() -> Iterator[None]:
    """Point the scratch sweep's VENUE process table at nothing, so an UNPINNED test is inert.

    ``resolve_scratch_sweep`` otherwise hands the sweep the machine's live
    ``/proc``, which makes a sweep test's verdict a property of whatever else is
    running: green in a container with no ``systemd --user``, red on a systemd
    host. A non-existent root fails the probe closed everywhere, so a test that
    forgot to pin fails deterministically instead of drifting by venue. Tests that
    MEAN to sweep pin their own table with ``tests._procfs.pinned_venue_proc``.
    """
    from teatree.core.retention import scratch  # noqa: PLC0415 — deferred: ORM import at fixture time

    with patch.object(scratch, "_VENUE_PROC", Path("/nonexistent-process-table-pin-your-own")):
        yield


@pytest.fixture(autouse=True)
def _reset_webhook_rate_limiter() -> Iterator[None]:
    """Drop the process-singleton webhook limiter so buckets don't leak across tests."""
    from teatree.core.views._rate_limit import reset_webhook_rate_limiter  # noqa: PLC0415

    reset_webhook_rate_limiter()
    yield
    reset_webhook_rate_limiter()


@pytest.fixture(autouse=True)
def _restore_django_settings_module() -> Iterator[None]:
    """Revert any ``DJANGO_SETTINGS_MODULE`` an in-process CLI test set process-globally.

    A test that invokes a ``t3`` typer command in-process (``ensure_django()``) sets
    ``DJANGO_SETTINGS_MODULE`` in ``os.environ`` and never restores it, leaking a value
    a LATER test's subprocess then inherits — the order-dependent shard/shuffle class the
    #3160 leak sentinel catches. Restore-only (snapshot-and-put-back, never touching the
    value at setup) so a well-behaved test is unaffected and only the leak is reverted.
    """
    before = os.environ.get("DJANGO_SETTINGS_MODULE")
    yield
    if before is None:
        os.environ.pop("DJANGO_SETTINGS_MODULE", None)
    else:
        os.environ["DJANGO_SETTINGS_MODULE"] = before


@pytest.fixture(autouse=True)
def _reset_forge_pr_budget_memo() -> Iterator[None]:
    """Reset the pk-keyed forge PR-budget memo around every test (TSH-1/TSH-7).

    ``pr_budget_forge._forge_cache`` is keyed on ``(ticket.pk, repo)``. Under
    sqlite ``TestCase`` rollback, rowids recycle, so a stale entry from an earlier
    test collides with a later test's fresh ticket and returns a bogus cached forge
    PR set — the 'green locally, red under a shard' pollution the budget-test
    classes were reactively patching in their own setUp. Resetting universally here
    is the durable fix for the whole pk-keyed-cache class.
    """
    from teatree.core.gates.pr_budget_forge import reset_forge_pr_budget_cache  # noqa: PLC0415

    reset_forge_pr_budget_cache()
    yield
    reset_forge_pr_budget_cache()


@pytest.fixture(autouse=True)
def _reset_claim_driving_registry() -> Iterator[None]:
    """Reset the pk-keyed claim-liveness registry around every test (#4164).

    ``claim_liveness._driving`` holds the task pks this process is executing. Under sqlite
    ``TestCase`` rollback rowids recycle, so a leaked entry makes a later test's fresh task
    read as still-executing and the sweep withholds the reap that test asserts it takes.
    """
    from teatree.core.claim_liveness import reset_driving_registry  # noqa: PLC0415 deferred, see #4164

    reset_driving_registry()
    yield
    reset_driving_registry()


@pytest.fixture(autouse=True)
def _reset_log_throttle() -> Iterator[None]:
    """Reset the process-local log-throttle memo around every test.

    ``throttled_log._last_warned`` records the last-``warning`` monotonic time per
    key. A leaked entry demotes a later test's throttled warning to ``debug``, so a
    test asserting the warning fires flakes by order. Its own reset helper is
    documented test-only; wire it into the roster so no individual test must
    remember to call it.
    """
    from teatree.utils.throttled_log import reset_throttle  # noqa: PLC0415

    reset_throttle()
    yield
    reset_throttle()


@pytest.fixture(autouse=True)
def _release_e2e_specs_claims() -> Iterator[None]:
    """Drop process-lifetime E2E specs checkout claims around every test.

    ``_e2e_specs_checkout._process_locks`` holds OPEN DESCRIPTORS, not values: the
    kernel releases them when a real CLI run exits, but a test process outlives every
    "run" it simulates. A leaked claim makes the next test's acquire of the same ref
    refuse as busy — held by itself — and leaks a descriptor per test.
    """
    release_process_locks()
    yield
    release_process_locks()


@pytest.fixture(autouse=True)
def _reset_work_tree_cwd_cache() -> Iterator[None]:
    """Drop the cwd → work-tree memo around every test.

    ``work_tree._tree_for`` memoises ``git rev-parse --show-toplevel`` per directory —
    correct for a real hook run, which is one process with one cwd. A test process runs
    many such runs and creates and destroys git repositories between them, so a leaked
    entry re-roots a later test's staged names against a tree that has since changed.
    """
    reset_cwd_cache()
    yield
    reset_cwd_cache()


@pytest.fixture(autouse=True)
def _reset_quote_blocklist_cache() -> Iterator[None]:
    """Reset the quote-scanner compiled-blocklist memo around every test (TSH-2/TSH-7).

    ``quote_scanner._BLOCKLIST_CACHE`` memoises compiled blocklist patterns keyed
    by resolved path and validated by ``(mtime_ns, size)``. A test that rewrites a
    blocklist at the same resolved path within one mtime tick at an identical size
    would otherwise read the earlier generation's patterns; clearing it here keeps
    one test's blocklist from leaking into another.
    """
    from teatree.hooks.quote_scanner import reset_blocklist_cache  # noqa: PLC0415

    reset_blocklist_cache()
    yield
    reset_blocklist_cache()


@pytest.fixture(autouse=True)
def _reset_seed_defaults_cache() -> Iterator[None]:
    """Reset the shipped seed-table parse memo around every test (TSH-2/TSH-7).

    ``seed_defaults._cache`` memoises the parsed ``defaults.toml`` for the loop / mode /
    schedule seeds, and both the seed loaders and the ``config_setting import`` classifier
    read it. Tests re-point ``DEFAULTS_TOML`` at a fixture, so a parse that outlived its
    test would classify a later import against the wrong shipped table.
    """
    from teatree.config.seed_defaults import (  # noqa: PLC0415 — deferred: conftest stays import-light at collection
        reset_seed_defaults_cache,
    )

    reset_seed_defaults_cache()
    yield
    reset_seed_defaults_cache()


@pytest.fixture(autouse=True)
def _isolate_scope_cache() -> Iterator[None]:
    """Reset the process-singleton token-scope cache with a no-op banner sink (PR-19).

    The cache persists for the loop-process lifetime, so without a per-test reset a
    ``missing_scope`` recorded in one test would short-circuit a later test's call,
    and its default banner sink would reach the DB-backed ``notify_user`` from a
    non-``django_db`` unit test. A no-op notifier keeps pure transport tests pure;
    tests that assert on the banner inject their own recorder.
    """
    import teatree.core.intake.scope_cache as _scope_cache  # noqa: PLC0415 — deferred: fixture-local reset of a process singleton

    _scope_cache._CACHE = _scope_cache.ScopeCache(notifier=lambda *_a, **_k: True)
    yield
    _scope_cache._CACHE = None


@pytest.fixture(autouse=True)
def _unset_review_skill_by_default() -> Iterator[None]:
    """Pin the #1539 reviewing-phase gate to its NO-OP unless a test opts in.

    The effective ``review_skill`` resolves through the DB-home config store
    (not Django settings), so a developer who configures one would otherwise see
    every reviewing-phase test refuse. Tests that exercise the gate re-patch
    ``configured_review_skill`` with a non-empty value inside their own scope.
    """
    from unittest.mock import patch  # noqa: PLC0415

    with patch("teatree.core.gates.review_skill_gate.configured_review_skill", return_value=""):
        yield


@pytest.fixture(autouse=True)
def _schema_readiness_current_by_default(request: pytest.FixtureRequest) -> Iterator[None]:
    """Pin the #3901 claim-admission gate to CURRENT unless a test opts in.

    The gate walks the live migration graph, which ``pytest --no-migrations`` leaves
    unrecorded while building the schema straight from the models — every claim would
    then read BEHIND and refuse. Stubbing the probe (not the verdict) also keeps the
    graph walk off every test's claim path. Tests that drive the gate re-patch
    ``pending_migrations`` inside their own scope; the two that need the REAL walk
    carry ``@pytest.mark.real_schema_readiness``.
    """
    from unittest.mock import patch  # noqa: PLC0415 — deferred: conftest stays import-light at collection

    from teatree.core.schema_readiness import (  # noqa: PLC0415 — deferred: importing it at collection pulls Django in
        invalidate_schema_readiness,
    )

    invalidate_schema_readiness()
    if "real_schema_readiness" in request.keywords:
        yield
    else:
        with patch("teatree.core.schema_readiness.pending_migrations", return_value=[]):
            yield
    invalidate_schema_readiness()


@pytest.fixture(autouse=True)
def _process_freshness_memo_isolated() -> Iterator[None]:
    """Drop the #4387 process-freshness memo around every test.

    The verdict is memoised per alias for 60 s and is read on the claim hot path, so a
    case that records an applied migration would otherwise leak its BEHIND verdict into
    the next test in the same xdist worker.
    """
    from teatree.core.process_freshness import (  # noqa: PLC0415 — deferred: importing it at collection pulls Django in
        invalidate_process_freshness,
    )

    invalidate_process_freshness()
    yield
    invalidate_process_freshness()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Create a minimal workspace structure with a main repo."""
    ws = tmp_path / "workspace"
    ws.mkdir()

    repo = ws / "my-project"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "manage.py").touch()

    return ws


@pytest.fixture
def project_info():
    """Shared ProjectInfo for tests that mock GitLab API calls."""
    from lib.gitlab import ProjectInfo  # noqa: PLC0415

    return ProjectInfo(project_id=42, path_with_namespace="org/repo", short_name="repo")


@pytest.fixture
def ticket_dir(workspace: Path) -> Path:
    """Create a ticket directory with a worktree inside the workspace."""
    td = workspace / "my-project-1234-test-fix"
    td.mkdir()

    wt = td / "my-project"
    wt.mkdir()
    # In worktrees, .git is a file (not a directory)
    (wt / ".git").write_text("gitdir: /some/path/.git/worktrees/my-project")

    return td


@pytest.fixture(autouse=True)
def _no_checkout_scan_outside_the_test(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """No pytest run may walk the real home looking for checkouts to reclaim (#4244).

    :func:`teatree.core.cleanup.checkout_registry.checkout_scan_roots` includes
    ``Path.home()`` unconditionally — correct in production, where the ad-hoc
    checkouts holding most of the reclaimable cache live there. Reached from a
    test it is two separate hazards: the walk is the runner's whole home, and the
    reapers downstream of it DELETE what they find, so a CI runner's own
    virtualenv is a candidate. Pinned to an empty directory; a test that needs
    real roots re-patches it inside its own scope, naming a tmp tree.
    """
    from unittest.mock import patch  # noqa: PLC0415 — deferred: conftest stays import-light at collection

    empty = tmp_path_factory.mktemp("no-checkouts")
    with patch("teatree.core.cleanup.checkout_registry.checkout_scan_roots", return_value=(empty,)):
        yield


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    """Clear the extension point registry between tests (legacy scripts only)."""
    try:
        from lib.registry import clear  # noqa: PLC0415
    except ImportError:
        yield
        return
    clear()
    yield
    clear()


@pytest.fixture(autouse=True)
def _no_live_aux_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """No pytest run may reach a live model through the aux one-shot seam.

    ``evals/README.md`` states the contract — pytest is the deterministic,
    no-live-model lane; the metered lane is ``t3 eval``. The aux seam
    (:func:`teatree.agents.one_shot.run_one_shot`) is the one place production
    code calls a model on an ordinary code path rather than through an agent
    spawn, and it is reached by the inbound Slack reading, the cheap answer
    builder, and the ticket short-describer. On a developer machine, where the
    ``claude`` child EXISTS, an unpatched call would run and bill; in CI it
    would merely be slow. Neither is a test result.

    Patched at :func:`~teatree.agents.harness.resolve_harness` — the single
    module attribute every caller funnels through, whichever name they imported
    ``run_one_shot`` under. A test that wants a turn injects its own ``harness=``
    (the documented DI parameter), which bypasses this entirely.
    """

    def _refuse() -> object:
        msg = "aux one-shot model turns are disabled under pytest; inject harness= or a reader seam"
        raise RuntimeError(msg)

    monkeypatch.setattr("teatree.agents.one_shot.resolve_harness", _refuse)


def pytest_configure(config: pytest.Config) -> None:
    """Arm the two cross-test thread sentinels for the whole suite.

    Always on, both for the same reason: each catches a failure that lands on a
    random bystander in a random shard, so a sentinel that has to be switched on
    could only ever be switched on after the fact. See ``tests/_thread_db_sentinel.py``
    (a worker thread that STRANDS a DB handle) and ``tests/_speak_thread_sentinel.py``
    (a test that LEAKS a local-playback thread into the next test, #4277).
    """
    config.pluginmanager.register(ThreadDbHandleSentinel(), "thread-db-handle-sentinel")
    config.pluginmanager.register(SpeakThreadSentinel(), "speak-thread-sentinel")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Prefer django.test.TestCase for DB-heavy tests; allow @pytest.mark.django_db on classes.

    Standalone functions with @pytest.mark.django_db should be grouped into
    TestCase classes when they share setup data (setUpTestData).  Class-based
    tests may use either TestCase or @pytest.mark.django_db depending on
    whether they need pytest fixtures (monkeypatch, tmp_path).

    See: souliane/teatree#98
    """
    for item in items:
        marker = item.get_closest_marker("django_db")
        if marker is None:
            continue
        cls = getattr(item, "cls", None)
        if cls is not None:
            continue  # class-based tests may use either pattern
        pytest.fail(
            f"{item.nodeid}: Standalone @pytest.mark.django_db functions "
            f"should be grouped into a TestCase class (see souliane/teatree#98)",
            pytrace=False,
        )


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate process env so tests cannot touch host workspace/config.

    There is no config file — every setting is DB-home, read from the canonical
    ``ConfigSetting`` store (Django-side) or via the Django-free ``cold_reader``
    (pre-Django hooks). Config isolation is therefore purely about the DB the cold
    reader resolves: clearing ``T3_CONFIG_DB`` and ``XDG_DATA_HOME`` (and redirecting
    ``$HOME``) leaves the cold reader with no config DB, so every setting fails OPEN
    to its dataclass default. A test that needs a cold-read value sets ``T3_CONFIG_DB``
    at a temp sqlite it seeds with a ``teatree_config_setting`` row. The update-check
    cache is redirected (below) at a hermetic per-test "up to date" verdict so the
    ``[update] …`` banner (``check_updates`` fails OPEN to ``True`` with no config DB)
    can never prepend non-JSON to a CLI's captured output.
    """
    home = tmp_path / "home"
    workspace = home / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    config_dir = tmp_path / "t3-hermetic-config"
    config_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / ".cache"))
    # The cold readers resolve the canonical ConfigSetting DB from these: clear both
    # so a cold reader resolves under the isolated ``$HOME`` (no DB → defaults) and
    # never reads a host DB.
    monkeypatch.delenv("T3_CONFIG_DB", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    # Redirect the update-check cache at a hermetic per-test dir holding a fresh
    # "up to date" verdict (empty message) so ``run_update_check`` short-circuits on the
    # cache before its network/subprocess ``gh`` call. ``check_updates`` fails OPEN to
    # ``True`` here (DB-home, no config DB), so without this a leaked subprocess mock in
    # the same xdist worker turns the ``gh`` result into a bogus ``[update] …`` banner
    # that prepends non-JSON to a CLI's captured output. The cache file is ``*.json`` (not
    # ``db.sqlite3``), so it never trips ``test_paths``' stale-DB scan. Update-check tests
    # set their own ``DATA_DIR`` per test, overriding this.
    update_cache_dir = config_dir / "update-check-cache"
    update_cache_dir.mkdir(parents=True, exist_ok=True)
    (update_cache_dir / "update-check.json").write_text(
        json.dumps({"ts": time.time(), "message": ""}), encoding="utf-8"
    )
    monkeypatch.setattr("teatree.update_check.DATA_DIR", update_cache_dir)
    # Default to per-worktree postgres for test isolation (override in specific tests)
    monkeypatch.setenv("T3_SHARE_DB_SERVER", "false")
    monkeypatch.delenv("T3_WORKSPACE_DIR", raising=False)
    monkeypatch.delenv("T3_BRANCH_PREFIX", raising=False)
    monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
    monkeypatch.delenv("T3_ORIG_CWD", raising=False)
    monkeypatch.delenv("TICKET_DIR", raising=False)
    monkeypatch.delenv("WT_VARIANT", raising=False)
    monkeypatch.delenv("COMPOSE_PROJECT_NAME", raising=False)
    # Loop control is DB-only: ``review_loop_enabled`` reads the DB ``LoopState``
    # tier and no env var, so ``T3_LOOPS_DISABLED`` is inert — there is nothing
    # to isolate here (the env-inertness is pinned by
    # ``tests/teatree_loop/test_review_loop_db_only_control.py``).
    # The external-outcome measure (#4506) reads the forge on any run with no fresh
    # snapshot, so an unstubbed reconciliation ledger would make a live `gh` call from
    # any test that runs it. Resolve to "no forge" here — the unmeasured state, which is
    # never scored healthy — and let the tests that exercise the read patch this back.
    monkeypatch.setattr(
        "teatree.core.factory.external_outcomes.resolve_forge",
        lambda overlay="": external_outcomes.Forge(host=None, repo_slugs=()),
    )


@pytest.fixture(scope="session")
def django_db_setup(
    request: pytest.FixtureRequest,
    django_db_blocker: object,
    worker_id: str,
    tmp_path_factory: pytest.TempPathFactory,
    *,
    django_db_keepdb: bool,
) -> Iterator[None]:
    """Migrate once per session, restore every xdist worker from that template.

    pytest-django's default ``django_db_setup`` runs a full ``migrate`` in
    *every* xdist worker process — with ``-n auto`` that is N redundant runs
    of the identical migration graph against the same ``:memory:`` sqlite
    target, each paying the full migration-graph cost before that worker's
    first DB test can run. This override instead builds ONE migrated
    template file — the first worker to grab the cross-process lock wins,
    see ``tests/_db_template.py`` — and restores every worker's private
    ``:memory:`` connection from it via ``sqlite3.Connection.backup()``
    instead of re-running migrations.

    The template is built the normal way (``call_command("migrate", ...,
    run_syncdb=True)``, matching what ``setup_databases`` does internally),
    so it includes the seed ``RunPython`` folded into ``0001_initial`` — the
    copy handed to each worker is byte-for-byte what a from-scratch migrate
    would have produced. ``FreshMigrateSeedsDefaultLoops`` re-migrates
    ``core`` from ``zero`` to prove the seed *inside a test*, on whatever
    connection it's given, so it is unaffected either way.

    Falls back to pytest-django's normal per-session ``setup_databases``
    when there is nothing to share: a plain non-xdist run (``worker_id ==
    "master"``, e.g. ``pytest -k foo`` with no ``-n``) or a non-memory
    backend (the template/backup trick is sqlite-specific).
    """
    from django.conf import settings  # noqa: PLC0415 — deferred until Django bootstraps
    from django.core.management import call_command  # noqa: PLC0415 — deferred until Django bootstraps
    from django.db import connections  # noqa: PLC0415 — deferred until Django bootstraps
    from django.test.utils import (  # noqa: PLC0415 — deferred until Django bootstraps
        setup_databases,
        teardown_databases,  # ty: ignore[unresolved-import]
    )

    # Dependency-only fixtures (test-environment patching, xdist NAME
    # suffixing) — pulled by value instead of as params to stay under the
    # repo's max-args=5 ceiling; both must still run before setup for the
    # same ordering pytest-django's own fixture relies on.
    request.getfixturevalue("django_test_environment")
    request.getfixturevalue("django_db_modify_db_settings")

    alias = "default"
    if worker_id == "master" or settings.DATABASES[alias]["NAME"] != ":memory:":
        with django_db_blocker.unblock():
            db_cfg = setup_databases(
                verbosity=request.config.option.verbose,
                interactive=False,
                keepdb=django_db_keepdb,
            )
        yield
        if not django_db_keepdb:
            with django_db_blocker.unblock():
                teardown_databases(db_cfg, verbosity=request.config.option.verbose)
        return

    template_dir_override = os.environ.get("TEATREE_TEST_DB_TEMPLATE_DIR")
    root_tmp_dir = Path(template_dir_override) if template_dir_override else tmp_path_factory.getbasetemp().parent
    template_path = root_tmp_dir / "django_test_template.sqlite3"
    lock_path = root_tmp_dir / "django_test_template.sqlite3.lock"

    def _build(path: Path) -> None:
        original_name = settings.DATABASES[alias]["NAME"]
        settings.DATABASES[alias]["NAME"] = str(path)
        connections[alias].close()
        try:
            call_command("migrate", verbosity=0, interactive=False, run_syncdb=True, database=alias)
        finally:
            connections[alias].close()
            settings.DATABASES[alias]["NAME"] = original_name

    with django_db_blocker.unblock():
        root_tmp_dir.mkdir(parents=True, exist_ok=True)
        build_or_reuse_template(template_path, lock_path, _build)
        target = connections[alias]
        target.ensure_connection()
        restore_from_template(template_path, target.connection)

    yield

    with django_db_blocker.unblock():
        connections.close_all()
