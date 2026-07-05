"""Pure data types shared across teatree modules.

These types have no Django dependencies and no imports from ``teatree.core``,
so they can be used by any layer without introducing cycles.
"""

import enum
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TypedDict

# The skill (requires) index: one ``{"skill": name, "requires": [...]}`` entry
# per SKILL.md. Mirrors ``teatree.skill_support.deps.SkillIndex`` — a local
# alias here so this foundation module needs no upward import.
type SkillIndexEntries = list[dict[str, object]]


class SlackVoiceClassifierMode(enum.StrEnum):
    """Strictness of the Slack voice/token mismatch classifier (#1395).

    Lives in :mod:`teatree.types` (no deps) so :mod:`teatree.config`
    can parse the DB-home ``slack_voice_classifier_mode`` setting
    without importing the classifier implementation in
    :mod:`teatree.backends.slack.voice_classifier` (the
    ``teatree.backends → teatree.config`` direction is forbidden by
    the tach module boundary, but ``teatree.config → teatree.types``
    is allowed).
    """

    STRICT = "strict"
    WARN = "warn"
    OFF = "off"

    @classmethod
    def parse(cls, value: str) -> "SlackVoiceClassifierMode":
        normalised = value.strip().lower()
        try:
            return cls(normalised)
        except ValueError as exc:
            valid = ", ".join(m.value for m in cls)
            message = f"Invalid slack_voice_classifier_mode {value!r}; valid values: {valid}"
            raise ValueError(message) from exc


class LocalPlayback(enum.StrEnum):
    """What plays through this machine's speakers (#2060).

    Lives in :mod:`teatree.types` (no deps) so :mod:`teatree.config` can
    parse the ``[teatree.speak] local`` setting without importing the
    :mod:`teatree.core.speak` implementation (the ``teatree.core →
    teatree.config`` edge is allowed but ``teatree.config →
    teatree.core`` would cycle).

    *   :attr:`OFF` (default) — nothing plays locally.
    *   :attr:`DM` — the bot→user DM texts play locally.
    *   :attr:`ALL` — DM texts AND the Stop-hook reading of in-client turn
        ends play locally.

    Independent of :attr:`SpeakConfig.slack`: Slack never auto-plays, so
    local playback is never suppressed by the Slack attach.
    """

    OFF = "off"
    DM = "dm"
    ALL = "all"

    @classmethod
    def parse(cls, value: object) -> "LocalPlayback":
        normalised = value.strip().lower() if isinstance(value, str) else value
        try:
            return cls(normalised)
        except ValueError as exc:
            valid = ", ".join(m.value for m in cls)
            message = f"Invalid speak local {value!r}; valid values: {valid}"
            raise ValueError(message) from exc


@dataclass(frozen=True)
class SpeakConfig:
    """The resolved ``[teatree.speak]`` sub-table — a local-playback enum + a slack bool (#2060).

    One cohesive object the config layer produces and :mod:`teatree.core.speak`
    reads. The two axes are fully independent:

    *   ``local`` — :class:`LocalPlayback`: what plays through this machine's
        speakers (macOS ``say``). :attr:`~LocalPlayback.OFF` nothing,
        :attr:`~LocalPlayback.DM` the bot→user DM texts,
        :attr:`~LocalPlayback.ALL` DM texts plus the in-client turn ends.
        Inert off macOS.
    *   ``slack`` — attach a spoken audio rendition to every Slack text DM the
        user receives, in the SAME message (one DM = text + inline audio
        player). Requires the Slack token's ``files:write`` scope. Applies to
        DMs only by nature; no scope interaction.

    The feature does something iff ``local`` is not off OR ``slack`` is on;
    the whole thing is additionally gated on the ``say`` binary being present
    (:func:`teatree.core.speak.binary_available`).
    """

    local: LocalPlayback = LocalPlayback.OFF
    slack: bool = False

    def enabled(self) -> bool:
        return self.local is not LocalPlayback.OFF or self.slack

    def speaks_dms(self) -> bool:
        return self.local in {LocalPlayback.DM, LocalPlayback.ALL}

    def speaks_in_client_turns(self) -> bool:
        return self.local is LocalPlayback.ALL

    def to_dict(self) -> dict[str, bool | str]:
        return {"local": self.local.value, "slack": self.slack}


class ScannerErrorClass(enum.StrEnum):
    """Classes of recoverable scanner failure surfaced to the dispatcher (#1287).

    Lives in :mod:`teatree.types` (no deps) so the messaging backend
    layer can raise it without creating a ``teatree.backends →
    teatree.loop`` import cycle. The :mod:`teatree.loop.scanners.base`
    module re-exports it for callers that already import from the
    scanner-protocol module.
    """

    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    MISSING_SCOPE = "missing_scope"
    NETWORK = "network"
    UNKNOWN = "unknown"


class ScannerError(RuntimeError):
    """A scanner failed with a recoverable upstream error (#1287).

    Raised by a scanner (or by a backend method a scanner calls) when an
    auth / rate-limit / missing-scope / network failure prevents it from
    returning a meaningful signal list this tick. The dispatcher
    (:func:`teatree.loop.domain_jobs._run_job`) catches it, records the
    error on the tick report, DMs the user once per day per
    ``(scanner, error_class)``, and skips THAT scanner for one tick —
    the rest of the tick continues. The next tick re-tries the failing
    scanner cleanly.

    The empty-return convention is preserved for the case it was meant
    for: genuinely empty data (no PRs, no approvals, no broadcasts).
    The bug this exception class fixes is the conflation of the two
    cases — previously a scanner that hit a 401 would return ``[]`` and
    the dispatcher would read that as "nothing to do".
    """

    def __init__(
        self,
        *,
        scanner: str,
        error_class: ScannerErrorClass,
        detail: str = "",
    ) -> None:
        self.scanner = scanner
        self.error_class = error_class
        self.detail = detail
        message = f"{scanner}: {error_class.value}"
        if detail:
            message = f"{message} ({detail})"
        super().__init__(message)


@dataclass(frozen=True)
class RunCommand:
    """Structured run command with explicit working directory.

    Used by ``OverlayBase.get_run_commands()`` to describe how each service
    is launched. Every service comes up via ``docker compose up`` — the
    overlay supplies argv + cwd metadata that other CLI verbs reuse
    (``t3 <overlay> run tests``, ``run backend``, ``run build-frontend``).
    The runner never spawns anything on the host.
    """

    args: list[str] = field(default_factory=list)
    cwd: Path | None = None


type RunCommands = dict[str, list[str] | RunCommand]


class SymlinkSpec(TypedDict, total=False):
    path: str
    source: str
    mode: str
    description: str


class ServiceSpec(TypedDict, total=False):
    shared: bool
    service: str
    compose_file: str
    start_command: list[str]
    readiness_check: str
    base_image: str
    """Name of a ``BaseImageConfig`` the service's container should use.

    Teatree resolves this to the single master tag at ``worktree provision`` and
    exports it as a compose env var so ``image: ${...}`` substitution works.
    """


@dataclass(frozen=True, slots=True)
class BaseImageConfig:
    """Declares a Docker image teatree builds once and shares across worktrees.

    Teatree tags each image with the single master tag ``{image_name}:base``
    — NOT per-lockfile.  The image is built once from the master clone's
    lockfile and reused by every worktree.  Code changes are picked up
    automatically via the worktree's ``.:/app`` volume mount, and dependency
    drift is reconciled at container start by the overlay's entrypoint
    (``uv sync`` against the branch's lockfile) — so a per-lockfile image tag
    would only duplicate a cache the entrypoint already keeps.

    *build_context* is an absolute path (the overlay resolves it — usually
    the master-repo root for that image's repo).  *dockerfile* and *lockfile*
    are resolved relative to it; *lockfile* is the build source (the master
    lockfile baked into the image) — it no longer feeds the tag.  *env_var*
    is the name core exports into the per-worktree env cache with the tag as
    value, so compose files can reference ``image: ${env_var}``.
    """

    image_name: str
    dockerfile: str
    lockfile: str
    build_context: Path
    env_var: str
    build_args: dict[str, str] = field(default_factory=dict)

    def image_tag(self) -> str:
        return f"{self.image_name}:base"


class DbImportStrategy(TypedDict, total=False):
    kind: str
    source_database: str
    shared_postgres: bool
    snapshot_tool: str
    restore_order: list[str]
    notes: list[str]
    worktree_repo_path: str


class SkillMetadata(TypedDict, total=False):
    skill_path: str
    remote_patterns: list[str]
    skill_index: SkillIndexEntries
    resolved_requires: dict[str, list[str]]
    skill_mtimes: dict[str, int]
    teatree_version: str


class ToolCommand(TypedDict, total=False):
    name: str
    help: str
    command: str
    arguments: list[str]


class ValidationResult(TypedDict):
    errors: list[str]
    warnings: list[str]


# Default MR title pattern enforced at ``pr create`` (#1540). Lives here (no
# deps) so both :mod:`teatree.config` (the ``mr_title_regex`` setting default)
# and :mod:`teatree.core.mr_metadata` (the gate logic) reference one source
# without a layering violation. The type set is the union of Conventional
# Commits (``feat|fix|chore|docs|refactor|test|perf|build|ci``) and the
# release-notes types some overlays narrow to (``improvement|config|techdebt``),
# so the same accurate label (``test``, ``techdebt`` …) passes whichever gate
# fires — this core default, or an overlay's narrower ``mr_title_regex`` that
# mirrors its own CI. An overlay still declares a stricter pattern when needed.
DEFAULT_MR_TITLE_REGEX = (
    r"^(feat|fix|improvement|config|techdebt|chore|docs|refactor|test|perf|build|ci)(\(.+\))?!?: .+"
)


@dataclass(frozen=True, slots=True)
class ProvisionStep:
    """One unit of work in a worktree's provisioning sequence.

    ``subprocess_only`` is the thread-safety contract that decides how
    :func:`teatree.core.step_runner.run_provision_steps` executes the callable
    (souliane/teatree#2244):

    - ``False`` (default) — the callable MAY touch the ORM (mutate the
        ``Worktree`` row, query a model). Django DB connections are per-thread,
        so it runs **in-process**, never on a worker thread. A worker-thread
        time-box here would write on a connection invisible to the caller
        ("database table is locked" under a test transaction). The cost: an
        in-process callable has no wall-clock ceiling, so it must not block
        indefinitely on a subprocess.
    - ``True`` — the callable is a **pure subprocess shellout that touches no
        ORM** (``uv sync``, ``uv pip install -e``). It is time-boxed on a daemon
        worker thread by the configured ``provision_step_timeout_seconds``
        ceiling, so a child blocked forever on its PIPE (a network stall) aborts
        loud with an actionable alert instead of hanging the whole provision.

    The default is the correctness-safe one: an unmarked step keeps the
    ORM-safe in-process behaviour. A step only lands on a worker thread by
    affirmatively asserting it is subprocess-only — so the dangerous mistake
    (an ORM callable on a worker thread) is opt-in and never accidental.

    ``skip_probe`` (souliane/teatree#2949) is an optional cheap precondition
    check: when set and it returns ``True``, :func:`run_provision_steps` skips
    the (expensive) callable entirely and records a successful, near-zero
    ``StepResult(skipped=True)`` — the mechanism that lets a re-provision of an
    already-current worktree finish in seconds instead of re-paying every step.
    A probe that raises is treated as "cannot tell, so don't skip" (the callable
    still runs) rather than crashing the provision.

    ``requires`` / ``produces`` are the dependency-DAG edges (PR-27, replacing
    the interim concurrency-group field). A step's ``produces`` names the
    resources it makes available (``{"env-cache"}``, ``{"app-db"}``); another
    step's ``requires`` names the resources it needs first.
    :func:`run_provision_steps` schedules steps in dependency order — a step
    runs only once every step producing a token it ``requires`` has succeeded —
    and runs steps with **no dependency path between them** concurrently (the
    ``subprocess_only`` ones on a bounded thread pool, ORM steps serially
    in-process, the same thread-safety contract as ``subprocess_only`` above). A
    ``requires`` token that no step ``produces`` is a misconfiguration and fails
    the run loud (fail-closed), never a silent skip. Independent steps (empty
    ``requires``/``produces``, the default) run concurrently when
    ``subprocess_only`` — declaring an edge is how an overlay serialises two
    steps that a shared resource orders.

    ``post_condition`` (PR-27) is an optional truth check evaluated **after** the
    callable runs: a step whose callable succeeds but whose ``post_condition``
    returns ``False`` (or raises) is recorded FAILED — so a step that "ran" but
    did not actually produce its resource halts a required-step run instead of
    reporting green. The aggregate of every step's ``post_condition`` is what the
    FSM's ``PROVISIONED`` guard and ``worktree status`` evaluate to decide a
    worktree is *really* provisioned (see
    :mod:`teatree.core.provision_postconditions`).

    ``heavy`` (souliane/teatree#2949) selects the step's time-box ceiling: a
    fast step (symlinks, settings, a compose override) defaults to a short
    ceiling so a hang surfaces in seconds, not half an hour; a heavy step (a DB
    import, a frontend build) opts into the long ceiling by setting this
    ``True``. See :func:`teatree.core.provision_timebox.resolve_step_timeout_seconds`.
    """

    name: str
    callable: Callable[[], None]
    required: bool = True
    description: str = ""
    subprocess_only: bool = False
    skip_probe: Callable[[], bool] | None = None
    requires: frozenset[str] = frozenset()
    produces: frozenset[str] = frozenset()
    post_condition: Callable[[], bool] | None = None
    heavy: bool = False


# ── Sync types (shared vocabulary between core and backends) ─────────

LAST_SYNC_CACHE_KEY = "teatree_followup_last_sync"
PENDING_REVIEWS_CACHE_KEY = "teatree_pending_reviews"

type RawAPIDict = dict[str, object]
type PREntryDict = dict[str, object]
#: One row from an ad-hoc ``db query`` SELECT — column name -> value. Keys
#: are dynamic (whatever the query SELECTs), so a fixed-key TypedDict cannot
#: model it; this alias is the typed home for that shape (#774).
type SqlRow = dict[str, object]


@dataclass(frozen=True, slots=True)
class ConflictedMR:
    """One open authored MR/PR the followup sweep found in merge conflict.

    Overlay-agnostic: the GitLab/GitHub backends translate their own raw
    payloads into this shape so :func:`teatree.core.sync.sync_followup` can
    surface conflicted open authored MRs identically across forges. Detection
    is report-only — the sweep never auto-resolves or auto-pushes (#78).
    """

    iid: int
    repo: str
    web_url: str
    title: str

    def to_dict(self) -> dict[str, int | str]:
        return asdict(self)


@dataclass(slots=True)
class SyncResult:
    prs_found: int = 0
    issues_found: int = 0
    tickets_created: int = 0
    tickets_updated: int = 0
    labels_fetched: int = 0
    prs_merged: int = 0
    prs_closed: int = 0
    reviews_synced: int = 0
    worktrees_cleaned: int = 0
    errors: list[str] = field(default_factory=list)
    conflicted_mrs: list[ConflictedMR] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class DiscussionSummary:
    status: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(slots=True)
class PREntry:
    url: str
    title: str
    branch: str
    draft: bool
    repo: str
    iid: int
    updated_at: str
    state: str = "opened"
    pipeline_status: str | None = None
    pipeline_url: str | None = None
    approvals: RawAPIDict | None = None
    discussions: list[DiscussionSummary] | None = None
    e2e_test_plan_url: str | None = None
    review_requested: bool | None = None
    reviewer_names: list[str] | None = None
    review_permalink: str | None = None
    review_channel: str | None = None
    notion_status: str | None = None
    notion_url: str | None = None
    draft_comments_pending: bool | None = None
    draft_comments_count: int | None = None
    approvals_dismissed_at: str | None = None
    dismissed_approvers: list[str] | None = None

    def to_dict(self) -> PREntryDict:
        result: PREntryDict = {}
        for k in self.__slots__:
            v = getattr(self, k)
            if v is None:
                continue
            if k == "discussions":
                result[k] = [d.to_dict() for d in v]
            else:
                result[k] = v
        return result


class SyncBackend(ABC):
    @abstractmethod
    def is_configured(self, overlay: object) -> bool: ...

    @abstractmethod
    def sync(self, overlay: object) -> SyncResult: ...
