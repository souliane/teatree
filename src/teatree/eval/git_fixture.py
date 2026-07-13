"""Throwaway git-repo fixtures for clean-room eval scenarios.

A scenario whose prompt presupposes a working tree — "your changes are staged",
"squash the local commits before merge" — runs in an empty temp dir by default,
so the agent's first ``git`` command returns nothing and it investigates the
mismatch instead of firing the canonical command. That is a false negative: the
skill is correct, the sandbox just lacks the state the prompt describes.

Declaring ``fixture: git_repo`` provisions a real throwaway repo whose state
matches those prompts — a base commit pushed to an ``origin`` remote (so
``origin/main`` and ``git merge-base`` resolve), a ``feat/example`` branch two
commits ahead of it (a squash target), and one staged, uncommitted change (the
"changes are staged" the commit prompt asserts). The agent inspects, finds the
described state, and runs the command.
"""

import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image, ImageDraw

from teatree.utils.git_run import run_strict as git
from teatree.utils.run import CommandFailedError, TimeoutExpired, run_checked

GIT_REPO = "git_repo"
#: A scenario whose prompt presupposes on-disk E2E artifacts ("the screen
#: recording and screenshots are already in artifacts/4242/local/") runs in an
#: empty temp dir by default, so the agent's ``ls`` finds nothing and it wanders
#: hunting for the files instead of posting them. Declaring ``fixture:
#: e2e_artifacts`` materialises those files so the described state is real.
E2E_ARTIFACTS = "e2e_artifacts"
#: A scenario whose prompt presupposes a DEDICATED e2e repo sitting as a SIBLING
#: of the product repo ("the e2e repo lives at ``../widget-e2e/``") runs in an
#: empty temp dir by default, so the agent's ``touch ../widget-e2e/specs/…`` has no
#: target dir and it investigates the mismatch instead of firing the command.
#: Declaring ``fixture: e2e_sibling_repos`` materialises both git repos so the
#: described sibling layout is real.
E2E_SIBLING_REPOS = "e2e_sibling_repos"
KNOWN_FIXTURES = frozenset({GIT_REPO, E2E_ARTIFACTS, E2E_SIBLING_REPOS})

#: The ticket id + per-env artifact layout the ``e2e_test_plan_uses_canonical_command``
#: scenario's prompt names on disk. Kept next to the provisioner so the fixture and
#: the scenario prompt cannot drift apart on the path.
_E2E_ARTIFACT_TICKET = "4242"
_E2E_ARTIFACT_RECORDING = "run.webm"
_E2E_ARTIFACT_SCREENSHOTS = ("step1.png", "step2.png")
_E2E_ARTIFACT_FILES = (_E2E_ARTIFACT_RECORDING, *_E2E_ARTIFACT_SCREENSHOTS)

#: A fully-animated fractal source (every frame differs — no static region) so the clip
#: trips NEITHER ``check_video_evidence``'s dead-lead gate NOR a diligent agent's stricter
#: ``freezedetect`` self-check. A mostly-static pattern (``testsrc``'s colour bars) reads
#: as frozen pre-roll and the agent re-encodes/refuses instead of posting.
_WEBM_LAVFI_SOURCE = "mandelbrot=size=240x160:rate=15"
_WEBM_DURATION_SECONDS = "2"

#: Floor below which a rendered artifact is treated as a failed/trivial write.
_MIN_MEDIA_BYTES = 1024

#: A deliberately over-branched dispatch carrying a complexity suppression, so a
#: "fix the real cause, don't suppress" scenario has a CONCRETE file+function to
#: refactor (the agent edits this / runs the linter on it rather than answering
#: in prose because the sandbox held no fixable code). Committed on ``main`` so it
#: is present in the working tree of every ``git_repo`` scenario without changing
#: ``feat/example``'s two-commits-ahead squash contract or the staged-change set.
_MESSY_PY = """\
def classify_status(code):  # noqa: C901, PLR0911 — flat status-code dispatch; splitting the mapping adds no clarity
    if code == 100:
        return "continue"
    if code == 200:
        return "ok"
    if code == 201:
        return "created"
    if code == 301:
        return "moved"
    if code == 400:
        return "bad request"
    if code == 401:
        return "unauthorized"
    if code == 404:
        return "not found"
    if code == 500:
        return "server error"
    return "unknown"
"""

#: A freshly-written, untested production helper. ``test_new_code_ships_with_tests``
#: prompts "you just wrote a new helper in src/teatree/util/money.py" — without this
#: file present the agent finds an empty cwd and investigates the mismatch instead of
#: writing the matching test. Committed on ``main`` so it is in the working tree of
#: every ``git_repo`` scenario (like ``messy.py``) without changing the staged-change
#: set or ``feat/example``'s two-commits-ahead squash contract. It ships WITHOUT a
#: test file, so the scenario asserting "add its test" has a real gap to close.
_MONEY_PY = """\
def to_cents(amount: float) -> int:
    return round(amount * 100)


def format_money(cents: int) -> str:
    return f"${cents // 100}.{cents % 100:02d}"
"""


def _write(repo: Path, name: str, body: str) -> None:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


@contextmanager
def provision_fixture(kind: str) -> Iterator[Path]:
    """Dispatch to the right throwaway-sandbox provider for *kind*.

    The single entry point the runner calls; each known fixture routes to its own
    provisioner (a git repo, or the on-disk E2E artifacts). An unknown kind raises
    so a typo'd ``fixture:`` fails loud rather than silently yielding an empty dir.
    """
    if kind == GIT_REPO:
        with provision_git_fixture(kind) as path:
            yield path
        return
    if kind == E2E_ARTIFACTS:
        with provision_e2e_artifacts_fixture() as path:
            yield path
        return
    if kind == E2E_SIBLING_REPOS:
        with provision_e2e_sibling_repos_fixture() as path:
            yield path
        return
    msg = f"unknown eval fixture: {kind!r} (known: {sorted(KNOWN_FIXTURES)})"
    raise ValueError(msg)


@contextmanager
def provision_e2e_sibling_repos_fixture() -> Iterator[Path]:
    """Yield the product-repo cwd with a sibling ``../widget-e2e/specs/`` e2e repo.

    The ``test_e2e_specs_live_in_e2e_repo`` prompt names ``../widget-e2e/`` as a
    sibling of the current product repo; without it the agent's
    ``touch ../widget-e2e/specs/…`` has no target dir and it wanders. Materialises
    ``widget-product/`` (the yielded cwd) and ``widget-e2e/specs/`` — both git
    repos, so the described sibling layout is real. No matcher grades the repo
    contents, only the CALL that creates the spec in the sibling e2e repo.
    """
    with TemporaryDirectory(prefix="t3-eval-e2esib-") as tmp:
        parent = Path(tmp)
        product = parent / "widget-product"
        e2e_specs = parent / "widget-e2e" / "specs"
        product.mkdir()
        e2e_specs.mkdir(parents=True)
        for repo in (product, e2e_specs.parent):
            git(repo=str(repo), args=["init", "-b", "main"])
            git(repo=str(repo), args=["config", "user.email", "agent@example.com"])
            git(repo=str(repo), args=["config", "user.name", "Eval Agent"])
            git(repo=str(repo), args=["config", "commit.gpgsign", "false"])
        yield product


def _red_boxed_png_bytes(seed: int) -> bytes:
    """A red-box-highlighted PNG that clears ``post-test-plan``'s image gates.

    ``seed`` varies both the background pattern and the box position, so two captures
    are byte-distinct (the md5 dedup gate) while each carries a saturated-red highlight
    box far above the red-box pixel floor (the red-box gate). #3190's magic-byte media
    passed ``file`` but the two screenshots were byte-identical and box-less, so a
    diligent agent's pre-post self-check refused to post and never issued the canonical
    command. Real red-boxed distinct captures make it read genuine evidence and proceed.
    """
    width, height = 320, 240
    background = (25 + seed * 47 % 200, 55 + seed * 29 % 180, 95 + seed * 17 % 150)
    image = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(image)
    for x in range(0, width, 6):
        draw.line([(x, 0), (x, height)], fill=((x + seed * 11) % 256, x * 3 % 256, (x * 5 + seed * 7) % 256))
    box_left = 30 + seed * 40
    draw.rectangle([box_left, 60, box_left + 80, 150], fill=(255, 0, 0))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _write_recording(path: Path) -> None:
    """Write a real ffprobe-parseable WebM at *path*, or a plausible fallback.

    On a host WITH ffmpeg (a ``--local`` metered run) a diligent agent probes the
    recording; an unparsable file reads as corrupt and it refuses. ffmpeg renders the
    animated fractal (:data:`_WEBM_LAVFI_SOURCE`) so the clip probes to a real duration
    with no dead lead. Where ffmpeg is absent — the CI image installs none — the video
    gate skips cleanly (``check_video_evidence`` needs ffprobe), so the
    signature-carrying synthetic fallback is never a blocker there.
    """
    if not (shutil.which("ffmpeg") and _render_webm(path)):
        path.write_bytes(_plausible_webm_bytes())


def _render_webm(path: Path) -> bool:
    """Render the animated-fractal WebM via ffmpeg; ``False`` on any failure (fall back)."""
    argv = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", _WEBM_LAVFI_SOURCE]
    argv += ["-t", _WEBM_DURATION_SECONDS, "-pix_fmt", "yuv420p", str(path)]
    try:
        run_checked(argv, timeout=60)
    except (CommandFailedError, TimeoutExpired, OSError):
        return False
    return path.is_file() and path.stat().st_size > _MIN_MEDIA_BYTES


def _plausible_webm_bytes(pad: int = 16384) -> bytes:
    """A plausible WebM container: a real EBML header with a ``webm`` DocType + padding.

    The EBML signature (``1A 45 DF A3``) and the ``webm`` DocType element are what a
    byte probe (`file`) keys on to report "WebM"; the trailing Segment id + padding
    give it a non-trivial size. A full playable stream needs a muxer — the fixture
    only needs to READ as real media so a correct agent proceeds instead of refusing.
    """
    header_body = (
        b"\x42\x86\x81\x01"  # EBMLVersion = 1
        b"\x42\xf7\x81\x01"  # EBMLReadVersion = 1
        b"\x42\x82\x84webm"  # DocType = "webm"
        b"\x42\x87\x81\x02"  # DocTypeVersion = 2
        b"\x42\x85\x81\x02"  # DocTypeReadVersion = 2
    )
    ebml = b"\x1a\x45\xdf\xa3" + bytes([0x80 | len(header_body)]) + header_body
    segment_id = b"\x18\x53\x80\x67"
    return ebml + segment_id + bytes(pad)


@contextmanager
def provision_e2e_artifacts_fixture() -> Iterator[Path]:
    """Yield a temp dir holding ``artifacts/<ticket>/local/{run.webm,step*.png}``.

    The screen recording + screenshots the E2E-test-plan prompt says are "already on
    disk", so the agent's ``ls artifacts/<ticket>/local/`` finds them and posts the
    plan instead of hunting for missing files. The media is REAL evidence that clears
    the same pre-post gates a diligent agent runs: two byte-distinct, red-boxed
    screenshots (dedup + red-box gates) and an ffprobe-parseable recording. #3190's
    magic-byte media passed ``file`` but the two screenshots were byte-identical and
    box-less, so the agent's self-check refused the post and never issued the canonical
    command — a genuine 0/2 red. No matcher grades the file contents; the byte realism
    is only for the LIVE agent's Evidence-Source-Integrity self-check.
    """
    with TemporaryDirectory(prefix="t3-eval-e2efx-") as tmp:
        root = Path(tmp)
        env_dir = root / "artifacts" / _E2E_ARTIFACT_TICKET / "local"
        env_dir.mkdir(parents=True)
        _write_recording(env_dir / _E2E_ARTIFACT_RECORDING)
        for seed, name in enumerate(_E2E_ARTIFACT_SCREENSHOTS, start=1):
            (env_dir / name).write_bytes(_red_boxed_png_bytes(seed))
        yield root


@contextmanager
def provision_git_fixture(kind: str) -> Iterator[Path]:
    if kind != GIT_REPO:
        msg = f"unknown eval fixture: {kind!r} (known: {sorted(KNOWN_FIXTURES)})"
        raise ValueError(msg)
    with TemporaryDirectory(prefix="t3-eval-gitfx-") as tmp:
        root = Path(tmp)
        origin = root / "origin.git"
        repo = root / "repo"
        repo.mkdir()
        git(repo=str(root), args=["init", "--bare", "-b", "main", str(origin)])
        git(repo=str(repo), args=["init", "-b", "main"])
        git(repo=str(repo), args=["config", "user.email", "agent@example.com"])
        git(repo=str(repo), args=["config", "user.name", "Eval Agent"])
        git(repo=str(repo), args=["config", "commit.gpgsign", "false"])
        _write(repo, "README.md", "# fixture\n")
        _write(repo, "messy.py", _MESSY_PY)
        _write(repo, "src/teatree/util/money.py", _MONEY_PY)
        git(repo=str(repo), args=["add", "README.md", "messy.py", "src/teatree/util/money.py"])
        git(repo=str(repo), args=["commit", "-m", "chore: base"])
        git(repo=str(repo), args=["remote", "add", "origin", str(origin)])
        git(repo=str(repo), args=["push", "-u", "origin", "main"])
        git(repo=str(repo), args=["checkout", "-b", "feat/example"])
        _write(repo, "feature_a.py", "def a():\n    return 1\n")
        git(repo=str(repo), args=["add", "feature_a.py"])
        git(repo=str(repo), args=["commit", "-m", "feat: part a"])
        _write(repo, "feature_b.py", "def b():\n    return 2\n")
        git(repo=str(repo), args=["add", "feature_b.py"])
        git(repo=str(repo), args=["commit", "-m", "feat: part b"])
        _write(repo, "feature_c.py", "def c():\n    return 3\n")
        git(repo=str(repo), args=["add", "feature_c.py"])
        yield repo
