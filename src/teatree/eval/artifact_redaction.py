"""Redact every credential value from an eval artifact before it is written.

Every eval report, dashboard and run log is uploaded from CI, and its content is
whatever the agent, a hook or a provider error happened to print, so a credential
in the run environment can end up in any of them. :func:`write_artifact` is the
only writer the eval code uses for those files, and it redacts first.

The credential set is every value the run could leak: the two Anthropic
credentials and the OpenAI-compatible key, each line of the ``EVAL_OAUTH_TOKENS``
pool, and every value :class:`~teatree.llm.credentials.Credential` resolved from
the ``pass`` store (which never reaches the environment) — each from 8 characters,
since a self-hosted key can be short — plus any other environment variable named
like a secret (``*KEY`` / ``*SECRET`` / ``*PASSWORD`` / ``*TOKEN(S)``) whose value
is at least 16 characters, so a short setting that merely ends in ``KEY`` does not
rewrite every report. A named value under 8 characters (a tag name, a lone quote,
a JSON literal such as ``null``) or all digits is never one, since no such key
exists, and redacting it would rewrite the artifact from the inside: a warning
names its variable instead. A digit-only value only its ``*KEY``-style name marks
is still one — its format is unknown, and a numeric password is plausible.

Each value is replaced raw, HTML-, JSON- and HTML-of-JSON-escaped, and
percent-encoded (path, URL and form style, upper- and lower-case hex), longest first. A transcript extractor
truncates hook output before anything is written, so a token can also arrive cut:
any 16-character window of a value is found by sliding a 16-character window over
the artifact's word runs and looking each one up in a precomputed set, which keeps
a multi-megabyte report linear. Unconfigured tokens with a known shape
(``sk-ant-``, ``gh[pousr]_``, ``github_pat_``) are redacted too. The marker is
``[REDACTED]``, which is inert in HTML, JSON and Markdown alike. As defense in
depth, every workflow upload is preceded by :func:`redact_in_place_main` over
exactly the files it publishes; it deletes any file it cannot prove clean on disk
(and any symlink, never followed) and fails the step each upload is gated on. The
workflows reach both filters through :func:`main` (``python -m
teatree.eval.artifact_redaction``), which resolves in an overlay's checkout too.

Known limits: a token cut to fewer than 16 characters stays visible, and the agent
itself still receives the credential it needs to log in. The job log relies on
GitHub's own masking plus :func:`tee_main`, the filter every eval run and every
artifact printed into the log is piped through.
"""

import argparse
import contextlib
import dataclasses
import html
import json
import os
import re
import sys
from collections.abc import Iterator, Mapping, Sequence
from functools import cached_property
from pathlib import Path
from urllib.parse import quote, quote_plus

from teatree.llm.credentials import AnthropicApiKeyCredential, AnthropicSubscriptionCredential, Credential
from teatree.llm.openai_compatible import OPENAI_COMPATIBLE_API_KEY_ENV

REDACTED = "[REDACTED]"

#: The newline-separated subscription-token pool the CI OAuth switch maintains.
OAUTH_POOL_ENV = "EVAL_OAUTH_TOKENS"

CREDENTIAL_ENV_VARS = (
    AnthropicSubscriptionCredential.spec.env_var,
    AnthropicApiKeyCredential.spec.env_var,
    OPENAI_COMPATIBLE_API_KEY_ENV,
)

#: The variable-name endings ``teatree.utils.run.redact_secrets`` treats as secrets.
SECRET_NAME_SUFFIXES = ("KEY", "SECRET", "PASSWORD", "TOKEN", "TOKENS")

#: The shortest secret-named env value treated as a credential, and the window a cut token is matched by.
MIN_SECRET_LENGTH = 16

#: The shortest named value treated as a credential. Below it a value is a tag name, an
#: attribute, a JSON literal or a lone quote as often as a key, and redacting it would
#: break the very artifact it protects, so it is left alone, with a warning.
MIN_NAMED_LENGTH = 8

_RESOLVED_ELSEWHERE = "a credential resolved from the pass store"

_WORD_CHARS = "A-Za-z0-9_-"
_WORD_RUN = re.compile(rf"(?<![{_WORD_CHARS}])[{_WORD_CHARS}]{{{MIN_SECRET_LENGTH},}}")
_PERCENT_ESCAPE = re.compile(r"%[0-9A-F]{2}")
_TOKEN_SHAPES = re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}")


def _cannot_be_a_secret(value: str) -> bool:
    return len(value) < MIN_NAMED_LENGTH or (value.isascii() and value.isdigit())


def _escaped_forms(value: str) -> set[str]:
    as_json = json.dumps(value)[1:-1]
    percent = {quote(value, safe=""), quote(value), quote_plus(value)}
    lower_hex = {_PERCENT_ESCAPE.sub(lambda escape: escape.group().lower(), form) for form in percent}
    return {value, html.escape(value), as_json, html.escape(as_json), *percent, *lower_hex}


@dataclasses.dataclass(frozen=True)
class Redactor:
    """Replace a fixed set of credential values, wherever and however they appear in a text."""

    values: frozenset[str]

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "Redactor":
        env = os.environ if environ is None else environ
        named: dict[str, str] = {}
        for source, value in (
            *((name, env.get(name, "")) for name in CREDENTIAL_ENV_VARS),
            *((OAUTH_POOL_ENV, line) for line in env.get(OAUTH_POOL_ENV, "").splitlines()),
            *((_RESOLVED_ELSEWHERE, value) for value in Credential.resolved_values()),
        ):
            if value.strip():
                named.setdefault(value.strip(), source)
        suffixed = {
            value.strip()
            for name, value in env.items()
            if name not in {*CREDENTIAL_ENV_VARS, OAUTH_POOL_ENV}
            and name.upper().endswith(SECRET_NAME_SUFFIXES)
            and len(value.strip()) >= MIN_SECRET_LENGTH
        }
        plain = {value for value in named if _cannot_be_a_secret(value)} - suffixed
        for source in sorted({named[value] for value in plain}):
            sys.stderr.write(
                f"artifact redaction: {source} is shorter than {MIN_NAMED_LENGTH} characters or all digits, "
                "which no credential is, so it is left unredacted\n"
            )
        return cls(frozenset((set(named) - plain) | suffixed))

    @cached_property
    def _whole_values(self) -> re.Pattern[str] | None:
        forms = sorted({form for value in self.values for form in _escaped_forms(value)}, key=len, reverse=True)
        return re.compile("|".join(map(re.escape, forms))) if forms else None

    @cached_property
    def _windows(self) -> frozenset[str]:
        return frozenset(
            run[start : start + MIN_SECRET_LENGTH]
            for value in self.values
            for run in _WORD_RUN.findall(value)
            for start in range(len(run) - MIN_SECRET_LENGTH + 1)
        )

    def redact(self, text: str) -> str:
        if self._whole_values is not None:
            text = self._whole_values.sub(REDACTED, text)
        text = _TOKEN_SHAPES.sub(REDACTED, text)
        return self._redact_cut_tokens(text) if self._windows else text

    def _redact_cut_tokens(self, text: str) -> str:
        pieces: list[str] = []
        cursor = 0
        for run in _WORD_RUN.finditer(text):
            for start, end in self._covered_spans(run.group()):
                pieces.extend((text[cursor : run.start() + start], REDACTED))
                cursor = run.start() + end
        pieces.append(text[cursor:])
        return "".join(pieces)

    def _covered_spans(self, run: str) -> Iterator[tuple[int, int]]:
        """The maximal spans of *run* covered by credential windows, left to right."""
        span: tuple[int, int] | None = None
        for start in range(len(run) - MIN_SECRET_LENGTH + 1):
            if run[start : start + MIN_SECRET_LENGTH] not in self._windows:
                continue
            end = start + MIN_SECRET_LENGTH
            if span is not None and start <= span[1]:
                span = (span[0], end)
                continue
            if span is not None:
                yield span
            span = (start, end)
        if span is not None:
            yield span

    def write(self, path: Path, text: str, *, append: bool = False) -> str:
        """Write the redacted *text* to *path* and return what was written."""
        redacted = self.redact(text)
        with path.open("a" if append else "w", encoding="utf-8") as handle:
            handle.write(redacted)
        return redacted

    def redact_in_place(self, path: Path) -> bool:
        """Rewrite *path* redacted and prove it on disk.

        Anything not proven clean is deleted, never left as it was — a symlink too,
        which is never followed out of the upload paths.
        """
        proven = False
        try:
            with contextlib.suppress(OSError):
                proven = not path.is_symlink() and self._rewritten_clean(path)
        finally:
            if not proven:
                path.unlink(missing_ok=True)
        return proven

    def _rewritten_clean(self, path: Path) -> bool:
        self.write(path, path.read_text(encoding="utf-8", errors="replace"))
        on_disk = path.read_text(encoding="utf-8", errors="replace")
        return self.redact(on_disk) == on_disk


def redact_artifact(text: str) -> str:
    return Redactor.from_environment().redact(text)


def write_artifact(path: Path, text: str, *, append: bool = False) -> None:
    """The one writer for every eval artifact: redact against the live credential set, then write."""
    Redactor.from_environment().write(path, text, append=append)


def tee_main(argv: Sequence[str] | None = None) -> int:
    """Copy stdin to stdout and optionally append it to a log, both redacted, line by line.

    Each line is written through and flushed as it arrives, so a run killed by its
    step timeout keeps everything it printed. Undecodable bytes are replaced rather
    than fatal: a filter crash would turn the pipeline red for the wrong reason.
    """
    parser = argparse.ArgumentParser(description="Redacting tee for eval run output.")
    parser.add_argument("log", nargs="?", type=Path, help="Append the redacted stream to this file as well.")
    log: Path | None = parser.parse_args(argv).log
    redactor = Redactor.from_environment()
    for raw in sys.stdin.buffer:
        line = raw.decode("utf-8", errors="replace")
        redacted = redactor.write(log, line, append=True) if log is not None else redactor.redact(line)
        sys.stdout.write(redacted)
        sys.stdout.flush()
    return 0


def redact_in_place_main(argv: Sequence[str] | None = None) -> int:
    """Redact every file matching the given paths (a glob may sit in the file name), in place.

    The last step before each eval upload: a defense-in-depth pass over exactly the
    files the upload publishes, so a file that reached the uploaded directory without
    going through :func:`write_artifact` still leaves redacted. Each file is re-read
    after its rewrite; one that could not be read, written or proven clean is deleted
    (or, when even that fails, named) and the pass exits non-zero once every file was
    tried, which the gated upload step reads as "ship nothing". An error that stops
    the pass itself (``MemoryError``, an interrupt) first deletes every file not yet
    proven clean, so none is left raw on disk. A pattern that matches nothing is not
    an error — the upload step reports a missing file itself.
    """
    parser = argparse.ArgumentParser(description="Redact eval artifacts in place before they are uploaded.")
    parser.add_argument("patterns", nargs="+", type=Path, help="Files, or a glob in the file name, to redact.")
    redactor = Redactor.from_environment()
    unproven = [
        path
        for pattern in parser.parse_args(argv).patterns
        for path in sorted(pattern.parent.glob(pattern.name))
        if path.is_symlink() or path.is_file()
    ]
    failed = 0
    try:
        while unproven:
            path = unproven[0]
            try:
                if not redactor.redact_in_place(path):
                    sys.stderr.write(f"redact in-place: deleted {path}: not a file it could redact and verify\n")
                    failed += 1
            except OSError:
                sys.stderr.write(f"redact in-place: could not delete {path}, which it could not redact and verify\n")
                failed += 1
            unproven.pop(0)
    finally:
        for path in unproven:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                sys.stderr.write(f"redact in-place: could not delete {path}, which it could not redact and verify\n")
    return 1 if failed else 0


_COMMANDS = {"tee": tee_main, "in-place": redact_in_place_main}


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m teatree.eval.artifact_redaction {tee,in-place} ...``, the eval workflows' entry point.

    It runs from the installed package, so it resolves alike for the teatree host and
    for an overlay calling a reusable eval workflow, whose checkout has no ``scripts/``.
    """
    command, *rest = list(sys.argv[1:] if argv is None else argv) or [""]
    if command not in _COMMANDS:
        sys.stderr.write(f"usage: python -m teatree.eval.artifact_redaction {{{','.join(_COMMANDS)}}} ...\n")
        return 2
    return _COMMANDS[command](rest)


if __name__ == "__main__":
    sys.exit(main())
