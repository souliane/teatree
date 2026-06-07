"""``t3 <overlay> retro review-findings`` — classify A/B/C and file class-C gates.

The deterministic scaffold behind the retro skill's enforcement-retrospective
(``skills/retro/SKILL.md`` § "Recurrence → Escalation", #1573). It fetches a
PR's review comments through the existing
:class:`~teatree.core.backend_protocols.CodeHostBackend`, fingerprints each finding
for dedup, records the agent-supplied A/B/C verdicts to a durable per-PR JSON
store, and files one scoped, deduped enforcement issue per class-C finding.

Two-pass workflow (the classification is judgement-heavy and supplied, never
guessed):

1. ``review-findings <pr-url>`` with no ``--classification`` lists every
    finding with its stable fingerprint, so the agent can read the diff + the
    existing gate set and decide A/B/C per fingerprint.
2. ``review-findings <pr-url> --classification verdicts.json`` records the
    verdicts and files the class-C enforcement issues (deduped — a re-run never
    refiles a fingerprint that already has an open issue).

``verdicts.json`` maps fingerprint → ``{"class": "A"|"B"|"C", "enforcement": "…"}``;
``enforcement`` is the smallest gate/test/hook that would prevent recurrence and
is required only for class-C.
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, TypedDict, cast

import typer
from django_typer.management import TyperCommand, command, initialize

from teatree.core.review_findings import (
    ClassifiedFinding,
    FilingContext,
    FindingClass,
    FindingsStore,
    ReviewFinding,
    parse_findings,
    process_review_findings,
)
from teatree.eval.gate_failures import (
    GateFailure,
    classify_gate_failure,
    escalate_gate_failures,
    extract_gate_failures,
    record_gate_failures,
)
from teatree.eval.session_transcript import parse_session_jsonl
from teatree.eval.transcript_resolver import resolve_transcript
from teatree.types import RawAPIDict
from teatree.url_classify import repo_and_iid

if TYPE_CHECKING:
    from teatree.core.backend_protocols import CodeHostBackend


class FindingView(TypedDict):
    fingerprint: str
    path: str
    line: int
    author: str
    body: str


class Command(TyperCommand):
    @initialize()
    def init(self) -> None:
        """``t3 <overlay> retro`` group root."""

    @command(name="review-findings")
    def review_findings(
        self,
        pr_url: str,
        *,
        classification: Annotated[
            str,
            typer.Option(help="Path to a JSON file mapping fingerprint -> {class, enforcement}."),
        ] = "",
        repo: Annotated[str, typer.Option(help="Override the repo slug parsed from the PR URL.")] = "",
        label: Annotated[str, typer.Option(help="Label applied to filed enforcement issues.")] = "enforcement-gap",
    ) -> str:
        """Classify a PR's review findings A/B/C and file class-C enforcement issues.

        With no ``--classification``, lists every finding + its fingerprint so
        the agent can supply verdicts. With ``--classification``, records the
        verdicts and files one deduped enforcement issue per class-C finding.
        Returns the structured result as JSON (the human-readable summary is
        written to stdout); ``call_command`` callers parse the JSON.
        """
        return json.dumps(self._run(pr_url, classification=classification, repo=repo, label=label))

    @command(name="gate-failures")
    def gate_failures(  # noqa: PLR0913 — django-typer command: every param is a CLI flag mapped 1:1 to the public --file/--session/--escalate/--repo/--pr-url/--label surface (same rationale as `ticket clear`), not an internal design smell.
        self,
        *,
        file: Annotated[
            str, typer.Option(help="Path to a session JSONL; defaults to the latest in-scope session.")
        ] = "",
        session: Annotated[str, typer.Option(help="A specific session id (in the cwd's project) to read.")] = "",
        escalate: Annotated[
            bool, typer.Option(help="File one deduped enforcement issue per recurring preventable failure.")
        ] = False,
        repo: Annotated[
            str, typer.Option(help="Repo slug to file the enforcement issue against (with --escalate).")
        ] = "",
        pr_url: Annotated[str, typer.Option(help="A PR/MR URL used to resolve the code host (with --escalate).")] = "",
        label: Annotated[str, typer.Option(help="Label applied to filed enforcement issues.")] = "enforcement-gap",
    ) -> str:
        """Extract a session's gate failures, classify them, record, and optionally escalate.

        A non-zero hook exit is a gate failure. The list pass classifies each
        preventable / environmental, records it to the durable store (so
        recurrence across sessions is observable), and emits JSON + a human
        summary. ``--escalate`` files one scoped, deduped enforcement issue per
        recurring preventable failure via the resolved code host. Returns the
        structured result as JSON.
        """
        transcript = resolve_transcript(
            latest=not (file or session),
            session=session or None,
            file=Path(file) if file else None,
        )
        context = FilingContext(repo=repo, pr_url=pr_url, label=label)
        return json.dumps(self._run_gate_failures(transcript, escalate=escalate, context=context))

    def _run_gate_failures(
        self,
        transcript: Path | None,
        *,
        escalate: bool,
        context: FilingContext,
    ) -> RawAPIDict:
        if transcript is None:
            self.stdout.write("  SKIP gate-failures: no session transcript found in scope")
            return {"skipped": True, "failures": [], "filed": []}

        events = parse_session_jsonl(transcript.read_text(encoding="utf-8", errors="replace"))
        failures = extract_gate_failures(events, session_id=transcript.stem)
        store = FindingsStore()
        record_gate_failures(store, failures)

        recurring = store.recurring_fingerprints(min_occurrences=2)
        views = self._gate_failure_views(failures, recurring=recurring)
        self._print_gate_failure_summary(views)

        result: RawAPIDict = {"skipped": False, "failures": views, "filed": []}
        if escalate:
            result["filed"] = self._escalate(failures, store=store, context=context)
        return result

    @staticmethod
    def _gate_failure_views(failures: list[GateFailure], *, recurring: set[str]) -> list[RawAPIDict]:
        views: list[RawAPIDict] = []
        for failure in failures:
            view = failure.as_dict()
            view["verdict"] = classify_gate_failure(failure).value
            view["recurring"] = failure.fingerprint in recurring
            views.append(view)
        return views

    def _print_gate_failure_summary(self, views: list[RawAPIDict]) -> None:
        self.stdout.write(f"  {len(views)} gate failure(s)")
        for view in views:
            recurring_mark = " (recurring)" if view["recurring"] else ""
            self.stdout.write(f"    {view['fingerprint']} [{view['verdict']}]{recurring_mark}: {view['gate']}")

    def _escalate(
        self,
        failures: list[GateFailure],
        *,
        store: FindingsStore,
        context: FilingContext,
    ) -> list[RawAPIDict]:
        host = self._resolve_host(context.pr_url)
        if host is None:
            self.stdout.write(f"  no code host resolved for {context.pr_url} — nothing escalated")
            return []
        filed = escalate_gate_failures(host, failures=failures, store=store, context=context)
        for item in filed:
            if item.withheld:
                self.stdout.write(f"    withheld ({item.withheld_reason}): {item.fingerprint}")
            else:
                state = "already filed" if item.already_filed else "filed"
                self.stdout.write(f"    {state}: {item.url}")
        return [
            {
                "fingerprint": item.fingerprint,
                "url": item.url,
                "already_filed": item.already_filed,
                "withheld": item.withheld,
                "withheld_reason": item.withheld_reason,
            }
            for item in filed
        ]

    def _run(self, pr_url: str, *, classification: str, repo: str, label: str) -> RawAPIDict:
        ref = repo_and_iid(pr_url)
        if ref is None:
            return {"error": f"Not a recognised PR/MR URL: {pr_url}"}
        parsed_repo, iid = ref
        repo_slug = repo or parsed_repo

        host = self._resolve_host(pr_url)
        if host is None:
            return {"error": f"No code host could be resolved for {pr_url}"}

        comments = host.list_pr_comments(repo=repo_slug, pr_iid=iid)
        findings = parse_findings(comments)
        store = FindingsStore()

        if not classification:
            return self._list_findings(pr_url=pr_url, findings=findings, store=store)

        return self._file_findings(
            findings=findings,
            store=store,
            host=host,
            classification_path=Path(classification),
            context=FilingContext(repo=repo_slug, pr_url=pr_url, label=label),
        )

    @staticmethod
    def _parse_verdicts(
        raw: RawAPIDict,
        findings: list[ReviewFinding],
    ) -> tuple[list[ClassifiedFinding], dict[str, str]]:
        by_fingerprint = {f.fingerprint: f for f in findings}
        classified: list[ClassifiedFinding] = []
        enforcement: dict[str, str] = {}
        for fingerprint, spec in raw.items():
            finding = by_fingerprint.get(str(fingerprint))
            if finding is None or not isinstance(spec, dict):
                continue
            verdict = cast("RawAPIDict", spec)
            class_str = str(verdict.get("class", "")).upper()
            if class_str not in FindingClass.__members__:
                continue
            classified.append(ClassifiedFinding(finding=finding, classification=FindingClass[class_str]))
            note = verdict.get("enforcement")
            if isinstance(note, str) and note:
                enforcement[finding.fingerprint] = note
        return classified, enforcement

    @staticmethod
    def _resolve_host(pr_url: str) -> "CodeHostBackend | None":
        from teatree.backends.loader import get_code_host_for_url  # noqa: PLC0415
        from teatree.core.overlay_loader import get_all_overlays  # noqa: PLC0415

        for overlay in get_all_overlays().values():
            host = get_code_host_for_url(overlay, pr_url)
            if host is not None:
                return host
        return None

    def _list_findings(
        self,
        *,
        pr_url: str,
        findings: list[ReviewFinding],
        store: FindingsStore,
    ) -> RawAPIDict:
        recurring = store.recurring_fingerprints()
        views: list[FindingView] = [
            {
                "fingerprint": f.fingerprint,
                "path": f.path,
                "line": f.line,
                "author": f.author,
                "body": f.body,
            }
            for f in findings
        ]
        self.stdout.write(f"  {len(views)} finding(s) on {pr_url}")
        for view in views:
            recurring_mark = " (recurring)" if view["fingerprint"] in recurring else ""
            self.stdout.write(f"    {view['fingerprint']}{recurring_mark}: {view['body'][:60]}")
        return {
            "pr_url": pr_url,
            "findings": views,
            "recurring": sorted(f.fingerprint for f in findings if f.fingerprint in recurring),
        }

    def _file_findings(
        self,
        *,
        findings: list[ReviewFinding],
        store: FindingsStore,
        host: "CodeHostBackend",
        classification_path: Path,
        context: FilingContext,
    ) -> RawAPIDict:
        if not classification_path.is_file():
            return {"error": f"Classification file not found: {classification_path}"}
        try:
            raw = json.loads(classification_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return {"error": f"Invalid classification JSON: {exc}"}
        if not isinstance(raw, dict):
            return {"error": "Classification JSON must be an object keyed by fingerprint."}

        classified, enforcement = self._parse_verdicts(raw, findings)
        summary = process_review_findings(
            host,
            classified=classified,
            enforcement=enforcement,
            store=store,
            context=context,
        )
        withheld = [f for f in summary.filed if f.withheld]
        self.stdout.write(
            f"  A={summary.counts['A']} B={summary.counts['B']} C={summary.counts['C']}"
            f" — filed {len(summary.filed) - len(withheld)} enforcement issue(s),"
            f" withheld {len(withheld)}"
        )
        for filed in summary.filed:
            if filed.withheld:
                self.stdout.write(f"    withheld ({filed.withheld_reason}): {filed.fingerprint}")
            else:
                state = "already filed" if filed.already_filed else "filed"
                self.stdout.write(f"    {state}: {filed.url}")
        return summary.as_dict()
