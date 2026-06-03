"""Pure data types shared across teatree modules.

These types have no Django dependencies and no imports from ``teatree.core``,
so they can be used by any layer without introducing cycles.
"""

import enum
import hashlib
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TypedDict


class SlackVoiceClassifierMode(enum.StrEnum):
    """Strictness of the Slack voice/token mismatch classifier (#1395).

    Lives in :mod:`teatree.types` (no deps) so :mod:`teatree.config`
    can parse the ``[teatree] slack_voice_classifier_mode`` setting
    without importing the classifier implementation in
    :mod:`teatree.backends.slack_voice_classifier` (the
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


class SpeakMode(enum.StrEnum):
    """What agent text is read aloud by the local text-to-speech seam (#1791).

    Lives in :mod:`teatree.types` (no deps) so :mod:`teatree.config` can
    parse the ``[teatree] speak_mode`` setting without importing the
    :mod:`teatree.core.speak` implementation (the ``teatree.core →
    teatree.config`` edge is allowed but ``teatree.config →
    teatree.core`` would cycle).

    The whole feature is additionally gated on the ``say`` binary being
    on ``PATH`` (:func:`teatree.core.speak.binary_available`); when it is
    absent the resolved mode is forced to :attr:`OFF` regardless of the
    configured value, so the feature is simply inert off macOS.

    *   :attr:`OFF` (default) — nothing is spoken.
    *   :attr:`IM_ONLY` — speak only text egressed to the user through the
        IM/DM channel abstraction (today the Slack DM in
        :func:`teatree.core.notify.notify_user`). The name is
        provider-neutral on purpose — it tracks the IM/DM channel, not
        the Slack backend.
    *   :attr:`ALL` — additionally speak every free-text agent reply (the
        Stop hook reads the transcript's last assistant text block).
    """

    OFF = "off"
    IM_ONLY = "im-only"
    ALL = "all"

    @classmethod
    def parse(cls, value: str) -> "SpeakMode":
        normalised = value.strip().lower()
        try:
            return cls(normalised)
        except ValueError as exc:
            valid = ", ".join(m.value for m in cls)
            message = f"Invalid speak_mode {value!r}; valid values: {valid}"
            raise ValueError(message) from exc


class SpeakTarget(enum.StrEnum):
    """Where spoken audio is delivered for the text-to-speech seam (#1791).

    Orthogonal to :class:`SpeakMode` (which decides *what* is spoken):
    one config pair controls the mode, this one the delivery surface so
    the same spoken text can reach the user's speakers, his phone, or both.

    *   :attr:`LOCAL` (default) — synthesise with the macOS ``say`` binary
        and play through the local speakers. Inert off macOS.
    *   :attr:`SLACK_AUDIO` — synthesise an audio file and upload it to the
        user's Slack DM so he hears it on his phone. Requires the Slack
        token's ``files:write`` scope (see
        :meth:`teatree.backends.slack_bot.SlackBotBackend.upload_audio_to_dm`).
    *   :attr:`BOTH` — deliver to local speakers *and* the Slack DM.
    """

    LOCAL = "local"
    SLACK_AUDIO = "slack-audio"
    BOTH = "both"

    @classmethod
    def parse(cls, value: str) -> "SpeakTarget":
        normalised = value.strip().lower()
        try:
            return cls(normalised)
        except ValueError as exc:
            valid = ", ".join(m.value for m in cls)
            message = f"Invalid speak_target {value!r}; valid values: {valid}"
            raise ValueError(message) from exc

    def includes_local(self) -> bool:
        return self in {SpeakTarget.LOCAL, SpeakTarget.BOTH}

    def includes_slack(self) -> bool:
        return self in {SpeakTarget.SLACK_AUDIO, SpeakTarget.BOTH}


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
    (:func:`teatree.loop.tick_jobs._run_job`) catches it, records the
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

    Teatree resolves this to a lockfile-hashed tag at ``worktree provision`` and
    exports it as a compose env var so ``image: ${...}`` substitution works.
    """


@dataclass(frozen=True, slots=True)
class BaseImageConfig:
    """Declares a Docker image teatree builds once and shares across worktrees.

    Teatree tags each image as ``{image_name}:deps-{sha256(lockfile)[:12]}`` —
    rebuild happens only when the lockfile content changes.  Code changes are
    picked up automatically via the worktree's ``.:/app`` volume mount, with
    no rebuild.

    *build_context* is an absolute path (the overlay resolves it — usually
    the main-repo root for that image's repo).  *dockerfile* and *lockfile*
    are resolved relative to it.  *env_var* is the name core exports into
    the per-worktree env cache with the resolved tag as value, so compose
    files can reference ``image: ${env_var}``.
    """

    image_name: str
    dockerfile: str
    lockfile: str
    build_context: Path
    env_var: str
    build_args: dict[str, str] = field(default_factory=dict)

    def image_tag(self) -> str:
        digest = hashlib.sha256((self.build_context / self.lockfile).read_bytes()).hexdigest()[:12]
        return f"{self.image_name}:deps-{digest}"


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
    trigger_index: list[dict[str, object]]
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
    name: str
    callable: Callable[[], None]
    required: bool = True
    description: str = ""


# ── Sync types (shared vocabulary between core and backends) ─────────

LAST_SYNC_CACHE_KEY = "teatree_followup_last_sync"
PENDING_REVIEWS_CACHE_KEY = "teatree_pending_reviews"

type RawAPIDict = dict[str, object]
type PREntryDict = dict[str, object]
#: One row from an ad-hoc ``db query`` SELECT — column name -> value. Keys
#: are dynamic (whatever the query SELECTs), so a fixed-key TypedDict cannot
#: model it; this alias is the typed home for that shape (#774).
type SqlRow = dict[str, object]


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
