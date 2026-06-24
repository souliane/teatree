# State-of-the-Art Cost-Efficient Agentic Eval Runner — Design Document

> Scope: `/Users/adrien/workspace/souliane/teatree/src/teatree/eval/`. All `file:line` references below were verified against the live tree on 2026-06-24. Doc URLs are cited inline.

## 1. Executive summary

The teatree eval runner is already **unusually mature** on cost *accounting*, anti-vacuity, isolation, and pass@k — it does not need a rewrite. The highest-leverage moves are small, surgical, and split cleanly into three buckets: (a) one foundational premise correction, (b) a handful of cheap reliability/quality/observability plumbing changes, and (c) a deliberate decision to *not* build several tempting-but-wrong features (Batch API, task budgets, aiohttp, `max_tokens:0` pre-warm).

The 3–5 highest-leverage moves, by impact-over-effort:

1. **Branch trial classification on `api_error_status` / stop_reason (infra vs config vs task failure).** Today a transient `529 overloaded_error` is retried 3× with no backoff then scored as a failure, and a permanent `402 billing_error` is silently retried 3× per scenario before the matrix gives up (`api_runner.py:203-239` only matches budget/turns; `cli/eval/multi_trial.py:329` retries any exception with no discrimination, no backoff). Fixing this removes the single largest source of false-red trials *and* fast-fails permanent config faults instead of burning the whole metered suite. **Win: pass-rate fidelity (no infra noise in pass@k) + avoids re-running the entire suite on a transient blip + fast-fail saves a doomed matrix on a wrong/over-quota key.** Effort: medium. Doc: <https://platform.claude.com/docs/en/api/errors>.

2. **Reasoning-first judge schema + drop judge effort to `low`/`medium` + cheaper judge tier.** The verdict schema lists `verdict` before `reason`, both required (`judge.py:67-75`), forcing the PASS/FAIL token *before* any reasoning — the exact inverse of Anthropic's grading guidance. The judge also inherits default `high` effort (`judge.py:178-192` sets no `effort`) and defaults to the same Sonnet tier as the run (`loader.py:35`), risking self-preference bias. **Win: higher judge accuracy (fewer mis-grades → fewer re-runs) at lower judge cost.** Effort: low. Docs: <https://platform.claude.com/docs/en/test-and-evaluate/develop-tests>, <https://platform.claude.com/docs/en/build-with-claude/effort>.

3. **Capture `session_id`, `stop_reason`, and `api_error_status` through the `message_mapping` seam.** These fields exist on the SDK messages but are dropped at `message_mapping.py:90-99`. They unlock per-trial traceability (replay/triage of flaky trials via the *existing* `transcript_resolver.find_session_file`), refusal detection, and context-window-exhaustion detection — all the reliability findings depend on this one plumbing change. **Win: every other reliability/quality fix becomes possible; flaky trials become replayable.** Effort: low.

4. **Add `model_context_window_exceeded` (and a refusal-aware terminal) to the dirty/cap classification.** Both `subagent_transcript.py:31` and `corpus_grade.py:27` define `_DIRTY_STOP_REASONS = {"max_tokens","refusal","error","aborted"}` and omit `model_context_window_exceeded`, so a context-exhausted trial is graded as a *clean finish*. **Win: clean pass-rate denominator; context-exhaustion no longer masquerades as task failure.** Effort: low. Doc: <https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons>.

5. **Thread `--parallel` into the metered pass@k / matrix lanes.** `parallel.py` has a bounded `ThreadPoolExecutor` (`DEFAULT_PARALLEL=1`, `MAX_PARALLEL=20`) but the high-volume CI lanes (`multi_trial.py:75` `run_pass_at_k_lane`, `collect_matrix_rows`) run scenarios in plain sequential comprehensions and never receive `parallel`. **Win: near-linear wall-clock reduction at zero extra per-scenario cost** (~hours → ~tens of minutes for a 211-scenario × k run, bounded by tier). Effort: medium; must land *with* move #1 (backoff) so concurrency does not create a 429 storm.

**Premise correction (load-bearing):** the task brief assumes the metered run authenticates via `ANTHROPIC_API_KEY`. The code authenticates via **subscription OAuth** (`auth.py:26` `OAUTH_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"`; `backends.py:7-9` "Neither bills an API key: both ride the subscription"). This eliminates *most* of the API-key-only caching/batch/rate-header levers: per-request `cache_control`/`ttl`, `inference_geo`, service tiers, the Batch API, `with_raw_response` headers, and the `anthropic-ratelimit-*` headers are all unreachable through the Agent-SDK→CLI transport teatree uses today. The cost ceiling on the subscription is the 5h/7d window, not RPM/ITPM/OTPM.

---

## 2. Ranked recommendations (sorted by impact-over-effort)

| # | Technique | Agentic? | Impact | Effort | Current teatree state | Action |
|---|-----------|----------|--------|--------|-----------------------|--------|
| 1 | Reasoning-first judge schema (CoT-then-verdict) | Yes (judge) | High | Low | Anti-pattern: `verdict` before `reason`, both required → `judge.py:67-75`; prompt is decision-first `judge.py:62-65` | Reorder schema so a `reasoning` string is emitted **first**; reword `_JUDGE_SYSTEM_PROMPT` to grade-then-decide |
| 2 | Judge on cheaper tier + low effort | Yes (judge) | High | Low | Judge default = Sonnet `loader.py:35`; `_judge_options` sets no `effort` → inherits `high` `judge.py:178-192` | Default judge to Haiku 4.5 (or distinct tier) at `effort="low"`; gate behind a judge-vs-judge agreement check |
| 3 | Capture `session_id`/`stop_reason`/`api_error_status` through mapping | Yes | High | Low | Dropped: `message_mapping.py:90-99` emits only `subtype/is_error/total_cost_usd/usage/model_usage` | Add the three fields to synthesized events; new `EvalRun.sdk_session_id` etc.; pin in conformance test |
| 4 | Error-type-aware retry (infra backoff vs config fast-fail) | Yes | High | Med | Absent: `_TERMINAL_MARKERS` covers only budget/turns `api_runner.py:203`; `multi_trial.py:329` retries any exc 3× w/ no backoff | Split retry on `api_error_status`: {429,500,502,503,529}→exp backoff; {400,401,402,403,404,413}→fast-fail whole run |
| 5 | `model_context_window_exceeded` as first-class terminal | Yes | High | Low | Absent from `_DIRTY_STOP_REASONS` `subagent_transcript.py:31`, `corpus_grade.py:27` → graded as clean finish | Add to both sets (DRY into one shared constant in `models.py`); add to `CAP_TERMINAL_REASONS` `models.py:14`; TDD test |
| 6 | Per-scenario effort tiering (easy scenarios @low) | Yes | High | Med | Mechanism fully wired (`model_variant.py:21`, `api_runner.py:566`); lane default pinned `high` `api_runner.py:115` | Benchmark per-scenario floor; pin `@low/@medium` in YAML for trivial probes; keep `high` representative default |
| 7 | Refusal detection + per-scenario refusal policy | Yes | High | Med | Subscription path classifies refusal `corpus_grade.py:112-120`; metered path drops `stop_reason` (depends on #3); no `refusal_policy` field | After #3, branch on `stop_reason=='refusal'`; add `EvalSpec.refusal_policy: fail\|excluded` |
| 8 | Flakiness flag (0<passes<k) | Yes | Med | Low | Partial: `pass_rate` computed `pass_at_k.py:79`; no FLAKY label in text/JSON | Derive label when `0<passes<trials`; surface in `multi_trial.py` summary + persist |
| 9 | Statistical-significance / margin on score-regression gate | Yes | High | Med | Strict inequality, no epsilon: `core/models/eval_run.py` `ScenarioRegression.regressed`; cost gate has tolerance `run_modes.py` | Require delta ≥ margin AND k ≥ min before flagging; or Wilson/Fisher one-sided test |
| 10 | Thread `--parallel` into pass@k/matrix lanes | Yes | High | Med | Partial: pool exists `parallel.py:31`; CI lanes use sequential comprehensions `multi_trial.py:75` | Thread `parallel`; size pool to host + window; land with #4 |
| 11 | Backfill `agent_sections` on more specs | Yes | High | Low | Mechanism present + fail-loud `context_budget.py`; many specs still resend whole 77 KB rules file | Audit which specs lack `agent_sections`; backfill — direct cold-write reduction |
| 12 | Capture `output_tokens_details.thinking_tokens` | Yes | Med | Low | Absent: `transcript.py:25-30` maps only 4 keys | Add nested read + `TokenUsage.thinking`; verify CLI forwards it |
| 13 | Capture per-step `AssistantMessage.usage` + `num_turns` | Yes | Med | Low | Dropped at `message_mapping.py:85-94` | Add per-turn usage (dedupe by `message_id`) + `num_turns`; diagnostic only |
| 14 | Cache-health tripwire (warn on low suite cache_hit_rate) | Yes | Med | Low | Measured not asserted: `benchmark.py:67-75` cache-hit%/cold-write% | Advisory warning below a floor; note cold-write% is dominated by per-process cold starts |
| 15 | `RateLimitEvent`/`api_error_status` → adaptive concurrency | Yes | Med | Low–Med | Dropped: `_collect` `api_runner.py:652-681` never inspects `RateLimitEvent` | Drain (not halt) the pool on `allowed_warning`/429; resume at `resets_at` |
| 16 | Add `CACHE_WRITE_MULTIPLIER_1H = 2.0` to pricing | Yes (latent) | Low | Low | Only 5m write modeled `pricing.py:14`; no 1h constant | Add constant + per-TTL split *iff* 1h ever surfaces; pure correctness guard |
| 17 | 1-hour cache TTL on shared prefix | Partial | Med | High (blocked) | Inert: no `cache_control`/`ttl` knob in `ClaudeAgentOptions`; CLI owns caching | Do not build now — unreachable via SDK transport on subscription |
| 18 | Cross-scenario warm prefix / pilot-then-fan-out | Partial | Med | High | Per-scenario prefix divergence (system + per-scenario tools); CLI auto-caches within-process only | Defer — needs prefix stabilization + reachable cache control |
| 19 | Wire dead `JudgeSpec.max_output_tokens` | Yes (judge) | Low | Low | Dead field: `models.py:166` default 512, never read in `judge.py` | Plumb into `_judge_options`, or delete; raise above 512 once reasoning-first lands |
| 20 | Plain `total_cost_usd` as headline cost | Yes | — | — | **Already done** `transcript.py:207-221`, `core/cost.py` prefers reported cost | None |
| 21 | Batch API (agentic scenario runs) | **No** | — | — | Absent, correctly | Do not adopt — cannot run an agent loop |
| 22 | Service / Priority Tier | No (cost) | — | — | Absent, correctly | Skip — paid commitment, not a discount; unreachable on subscription |
| 23 | `task_budget`, server compaction, context editing | No (transport) | — | — | Absent | Skip — Messages-API-only; excluded on Claude Code surfaces |
| 24 | aiohttp backend, `max_tokens:0` pre-warm, Tool Search, fine-grained streaming, token-efficient-tools | No | — | — | Absent | Skip — wrong layer / built-in / counterproductive (see §5) |

---

## 3. Already state-of-the-art in teatree

The runner already implements, correctly, a set of practices that most eval harnesses lack. These are the parts to protect, not change.

- **Cache-aware billed-input accounting.** `pricing.py:11,14` pins `CACHE_READ_MULTIPLIER=0.1` / `CACHE_WRITE_MULTIPLIER=1.25` as a single source of truth; `models.py:262-264` derives `effective_billed_input = input + 1.25*cache_creation + 0.10*cache_read`; `transcript.py:25-30` maps all four usage keys separately with malformed-key defense. These match the live pricing doc exactly (<https://platform.claude.com/docs/en/about-claude/pricing>).
- **Price-table-free warm-equivalent diagnostic.** `cost_fit.py:63-108` recovers `(base_in_rate, out_rate)` by least-squares with a `_MIN_CLEAN_CELLS=4` guard and an ill-conditioning guard that returns `None` rather than fabricating — genuinely best-in-class honest cost attribution. `warm_equivalent_cost` (`cost_fit.py:93`) quantifies exactly the upside any caching change would chase.
- **Main-vs-auxiliary cost split.** `transcript.py:306-339` reads per-model `model_usage[*].costUSD` and separates the requested model (`main_*`) from Claude Code's always-on `claude-haiku-4-5` background (`aux_*`), surfaced as `aux_cost_fraction` in `benchmark.py:83-90` — essential for fair model@effort comparison.
- **Cache-hit% / cold-write% observability.** `benchmark.py:67-75` computes token-weighted `cache_hit_rate` (sum-then-divide, not a mean of ratios) and `cold_write_fraction`.
- **pass@k / pass^k with cap-taint guard.** `pass_at_k.py:84-97` implements `require="any"`/`"all"` and force-fails a trial whose `terminal_reason` is in `CAP_TERMINAL_REASONS` (`models.py:14`) — a correctness detail the published guidance does not even mention (<https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents>).
- **Anti-vacuity guards (exceeds the docs).** `matcher_vacuity.py:51` flags negative-matcher-without-positive-anchor at load; `negative_control.py` plants a known violation and verifies the harness catches it; `skip_guard.py` raises on all-skipped / `$0`-metered / judge-graded-zero; judge absent/malformed verdict → FAIL (`judge.py:210-223`). This is the model to copy elsewhere.
- **Per-run isolation.** `isolation.py:31` redirects `HOME`/`XDG_CONFIG_HOME`/`CLAUDE_CONFIG_DIR` to a fresh temp dir; `api_runner.py` sets `setting_sources=[]`, `strict_mcp_config=True`; `ephemeral_checkout.py` provisions a detached worktree per scenario. This directly satisfies the docs' "clean environment per trial / no correlated failures" requirement.
- **Effort fully wired end-to-end.** `model_variant.py:21` `EFFORT_LEVELS`, threaded to `ClaudeAgentOptions.effort` at `api_runner.py:434`, per-scenario `@effort` wins over lane default (`api_runner.py:566`). The benchmark lane already A/Bs efforts.
- **Streaming by construction.** Both runner (`api_runner.py:669`) and judge (`judge.py:198`) consume the SDK via `async for`, so the docs' 10-min non-stream timeout risk does not apply.
- **Token-efficient tool use** is built-in on Claude 4+ — nothing to enable (`SdkBeta` is `Literal["context-1m-2025-08-07"]` only; no header needed).

---

## 4. High-impact additions

Each item below names the integration point, the exact API param/field, a doc citation, and the expected win.

### 4.1 Error-type-aware retry classification (impact: High)

- **Integration point:** read `ResultMessage.api_error_status` (and `.errors`) in `_collect` (`api_runner.py:652-681`) / `message_mapping.py`; split the retry policy in `_resilient_matrix_trial` (`cli/eval/multi_trial.py:329`, currently `for attempt in range(MAX_MATRIX_CELL_RETRIES + 1)` with no backoff, no discrimination).
- **API fields/codes:** `api_error_status: int | None` on the SDK `ResultMessage`. Canonical map (<https://platform.claude.com/docs/en/api/errors>): `429 rate_limit_error`, `529 overloaded_error`, `500 api_error`, `401 authentication_error`, **`402 billing_error`** (note: 402, not 403), `403 permission_error`, `404 not_found_error`, `400 invalid_request_error`, `413 request_too_large`, `504 timeout_error`. A `429` response carries a `retry-after` header — honor it when present.
- **Action:** retryable-infra `{429,500,502,503,529}` → exponential backoff with jitter, then record `errored` (not a scored fail) only after the bound; permanent-config `{400,401,402,403,404,413}` → do **not** retry, fail the whole run fast and loud. Keep `classify_terminal_error` (`api_runner.py:227`) as the budget/turns cap gate. Caveat: `api_error_status` may be populated only on `subtype=success`; mid-loop errors may surface as the bare `Exception` in `_collect` or as `subtype=error_during_execution` with the status in `.errors`, so read the structured field *and* fall back to parsing.
- **Win:** removes the dominant false-red source from pass@k; converts a wrong/over-quota key from N×3 wasted SDK spawns into a single fast failure.

### 4.2 Reasoning-first judge schema (impact: High)

- **Integration point:** `_VERDICT_SCHEMA` (`judge.py:67-75`) and `_JUDGE_SYSTEM_PROMPT` (`judge.py:62-65`).
- **API param:** `output_format = {"type":"json_schema","schema":...}` (`judge.py:191`). Structured outputs emit required properties first, in schema order — so placing `reasoning` first guarantees CoT tokens before the constrained PASS/FAIL token.
- **Doc:** "Encourage reasoning: Ask the LLM to think first before deciding an evaluation score" — <https://platform.claude.com/docs/en/test-and-evaluate/develop-tests>; ordering semantics — <https://platform.claude.com/docs/en/build-with-claude/structured-outputs>.
- **Action:** add a required `reasoning` string as the **first** property, then `verdict`, then `reason`; reword the prompt to grade-then-decide. `StructuredVerdict.from_structured_output` (`judge.py:110-120`) already tolerates extra fields. Pair with raising the (currently dead) `max_output_tokens` cap so the CoT+verdict object never truncates mid-JSON (truncation → no `structured_output` → spurious FAIL, `judge.py:216-217`).
- **Win:** measurably higher accuracy on nuanced rubrics (faithfulness/tone); the current verdict-first ordering is documented to *degrade* complex-judgement grading.

### 4.3 Cheaper, distinct-tier, low-effort judge (impact: High)

- **Integration point:** `DEFAULT_JUDGE_MODEL` (`loader.py:35`), `JudgeSpec.model` default (`models.py:165`), `_judge_options` (`judge.py:178-192`).
- **API param:** `effort="low"` on the judge's `CleanRoomConfig`; per the effort doc, `low` is recommended for "simple classification tasks" and Sonnet 4.6 should be set to `medium` explicitly to avoid latency (<https://platform.claude.com/docs/en/build-with-claude/effort>).
- **Doc:** "best practice to use a different model to evaluate than the model used to generate" — <https://platform.claude.com/docs/en/test-and-evaluate/develop-tests>.
- **Action:** default judge to `claude-haiku-4-5` (or any tier ≠ the run model + its `FALLBACK_MODEL`) at `effort="low"`; add a `JudgeSpec.effort` field for per-scenario opt-up. Gate behind a judge-vs-judge agreement check on the existing corpus (`confusion_matrix.py`, `corpus_grade.py` already exist) before flipping the default.
- **Win:** 3× cheaper judge input/output + faster (Haiku is the fastest tier) + removes self-preference bias, at unchanged verdict quality for the overwhelming majority of rubrics.

### 4.4 Capture `session_id` / `stop_reason` / `api_error_status` (impact: High — enabler)

- **Integration point:** `_message_to_event` (`message_mapping.py:90-99`) currently emits `{type,subtype,is_error,total_cost_usd,usage,model_usage}` for `ResultMessage` and `{type,message,parent_tool_use_id}` for `AssistantMessage` — dropping `session_id`, `stop_reason`, `api_error_status`, per-step `usage`, `message_id`, `num_turns`.
- **API fields:** verified present on SDK v0.2.94 — `ResultMessage.{session_id, stop_reason, api_error_status, num_turns}`, `AssistantMessage.{session_id, message_id, stop_reason, usage}`.
- **Doc:** request-id / traceability rationale — <https://platform.claude.com/docs/en/cli-sdks-libraries/sdks/python>; cumulative per-query usage — <https://code.claude.com/docs/en/agent-sdk/cost-tracking>.
- **Action:** add these to the synthesized events; add `EvalRun.sdk_session_id` (label it clearly — it is the Agent-SDK session id, **not** the Anthropic HTTP request-id) and surface it on RED trials. The consumer side already exists: `transcript_resolver.find_session_file(session_id)` resolves straight to the on-disk transcript.
- **Win:** every flaky/RED trial becomes replayable and locatable; unblocks refusal (#4.5) and context-window (#4.6) detection.

### 4.5 Refusal detection + per-scenario policy (impact: High)

- **Integration point:** after #4.4 lands, branch on `stop_reason=='refusal'` in `extract_terminal_reason` (`transcript.py:191-205`, currently reads only the result subtype). Reuse `_DIRTY_STOP_REASONS` (already includes `refusal` on the subscription/transcript path, `corpus_grade.py:27`).
- **API field:** `stop_reason` (branch on it directly, never on `stop_details`/`content`, which may be null). Refusals before output are billed `$0`.
- **Doc:** <https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback>; "Branch on stop_reason, not on stop_details or content."
- **Action:** add `EvalSpec.refusal_policy: Literal["fail","excluded"] = "fail"`. For `excluded`, route the trial like a *skip* (out of the pass@k denominator, `pass_at_k.py:137`); for adversarial/safety scenarios keep `fail` (refusal there is the intended pass). Surface refusal counts separately so an all-refused scenario is never silently green. Do **not** auto-retry on a fallback model — that would mask the refusal in the pass-rate.
- **Win:** benign security/life-science scenarios that trip the classifier stop depressing pass-rate; safety scenarios can score a refusal as correct.

### 4.6 `model_context_window_exceeded` as a first-class terminal (impact: High)

- **Integration point:** `_DIRTY_STOP_REASONS` in both `subagent_transcript.py:31` and `corpus_grade.py:27` (byte-identical literals that will drift — DRY into one shared constant in `models.py`); and `CAP_TERMINAL_REASONS` (`models.py:14`).
- **API field:** `stop_reason == "model_context_window_exceeded"` — a distinct HTTP-200 terminus on Claude 4.5+, separate from the user-set `max_tokens` cap.
- **Doc:** <https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons>.
- **Action:** add the value to the dirty set (→ `is_error=True` instead of "clean finish") **and** to `CAP_TERMINAL_REASONS` (→ treated as a non-pass, non-clean cap like `max_turns`, and excluded from the clean cost-fit at `benchmark.py:122-134` since it paid a partial cost). Surface it as its **own** labelled outcome bucket, distinct from both `max_tokens` and a real matcher/judge failure. TDD per CLAUDE.md: synthetic transcript whose final assistant `stop_reason` is the value → assert `is_error=True`.
- **Win:** clean pass-rate denominator; the three terminal stop reasons (`max_tokens` truncation / context-exhaustion / refusal) become genuinely three-way in the report.

### 4.7 Thread `--parallel` into pass@k / matrix lanes (impact: High)

- **Integration point:** `run_pass_at_k_lane` and `collect_matrix_rows` (`cli/eval/multi_trial.py:75`, `:301-305`) currently run sequential comprehensions and never receive `parallel`; `parallel.py:31` `run_specs` (the bounded pool, `MAX_PARALLEL=20`) is only wired into the single-trial path.
- **Doc:** "parallel sessions in the same directory build matching prefixes and read each other's cache" — <https://code.claude.com/docs/en/prompt-caching> (cache benefit is incidental on subscription; the real win is wall-clock).
- **Action:** thread `parallel` through both lanes (make `run_specs` pass@k-aware). Land **with** #4.1 (backoff) and #4.15 (adaptive throttle). Note: per-scenario isolation already holds under concurrency (`isolation.py`, `ephemeral_checkout.py`); on subscription the binding constraint is the 5h/7d window, not RPM, so cap modestly and watch host CPU/RAM (each scenario is a full Node `claude` process).
- **Win:** a ~211-scenario × k run drops from hours to tens of minutes at zero extra per-scenario cost.

### 4.8 Per-scenario effort tiering + observability (impact: High)

- **Integration point:** scenario YAML `model:`/`@effort`; the mechanism is fully wired (`api_runner.py:566`). Add `output_tokens_details.thinking_tokens` to `transcript.py:25-30` to make tiering data-driven.
- **Doc:** effort affects all token spend incl. tool calls; "step down to medium/low only when you've measured that the lower level holds quality on your evals" — <https://platform.claude.com/docs/en/build-with-claude/effort>.
- **Action:** keep the metered lane default `high` (representative of production). Use the benchmark lane to find each scenario class's floor; pin `@low/@medium` for trivial trigger/structural probes, reserve `@high/@xhigh` for delegation/TDD scenarios. Capture `thinking_tokens` so you can see which scenarios are reasoning-heavy.
- **Win:** lower output-token spend on easy scenarios with negligible fidelity loss; faster wall-clock.

### 4.9 Statistical-significance margin on the score-regression gate (impact: High)

- **Integration point:** `ScenarioRegression.regressed` in `core/models/eval_run.py` returns a strict `candidate_pass_rate < baseline_pass_rate` — no epsilon, no min-k. The cost gate already has a tolerance (`run_modes.py` `DEFAULT_COST_REGRESSION_TOLERANCE=0.20`); the score gate has none.
- **Doc:** the agents post explicitly declines to give a fixed threshold but frames success criteria around meaningful effect size — <https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents>; <https://platform.claude.com/docs/en/test-and-evaluate/develop-tests>.
- **Action:** require both (a) `candidate` below `baseline` by ≥ a configurable margin AND (b) `k ≥ min` before flagging; or a one-sided two-proportion test (Wilson/Fisher) that only flags when the CI excludes equality.
- **Win:** eliminates the largest false-positive source in the regression gate (a 4/5→3/5 flip at k=5 is statistically indistinguishable today and reds the merge).

---

## 5. Does NOT apply, and why

These sound good but are wrong for this runner — documented here so they are not revisited.

- **Batch API for the agentic scenario runs.** A batch request is a stateless single-shot Messages call; it cannot drive a multi-turn tool-use loop with sub-agents (each scenario is a full Agent-SDK session, `api_runner.py`). It also disables the within-turn cache the run relies on, and batch latency is up to 24h — a CI gate needs minutes. The docs' Managed Agents page states plainly "Batch API discount — does not apply: Sessions are stateful and interactive." (<https://platform.claude.com/docs/en/about-claude/pricing>) **Honest reason: there is no agent loop in a batch.** (A future *decoupled* judge-only pass could batch for 50% off — but the cheaper-Haiku-judge change captures most of that saving synchronously without async plumbing.)

- **Service / Priority Tier.** Priority Tier is a *paid capacity commitment* (Contact Sales, 1/3/6/12-month), not an on-demand discount; the default `service_tier="auto"` is free. It is also a Messages-API request param, unreachable through the CLI transport, and the lane is subscription-billed. **Reason: not a cost lever, and not reachable.** (<https://platform.claude.com/docs/en/api/service-tiers>)

- **`task_budget`, server-side compaction (`compact_20260112`), context editing (`clear_tool_uses`/`clear_thinking`).** All are Messages-API `output_config`/`context_management` fields. The docs explicitly exclude task budgets on "Claude Code or Cowork surfaces," and teatree runs every scenario through the bundled CLI, which exposes none of these. Most eval scenarios are also short (turn/budget-capped) and never approach the 150k/100k triggers. **Reason: Messages-API-only; unreachable via the SDK→CLI path; would require re-implementing the agent loop.** (<https://platform.claude.com/docs/en/build-with-claude/task-budgets>, <https://platform.claude.com/docs/en/build-with-claude/compaction>)

- **`excludeDynamicSections` / `--exclude-dynamic-system-prompt-sections`.** This flag neutralizes per-machine sections the `claude_code` *preset* embeds. Teatree uses a **custom string** system prompt (`api_runner.py:419` via `--system-prompt-file`), which contains exactly the bytes teatree wrote and embeds no machine-specific sections. **Reason: teatree is already immune to the problem this flag solves; adopting it is a no-op, and switching to the preset "for caching" would *introduce* the problem.**

- **aiohttp backend (`anthropic[aiohttp]` + `DefaultAioHttpClient`).** This is a transport-efficiency lever on the raw `AsyncAnthropic` client. Teatree never instantiates that client — the agent SDK spawns the Node `claude` CLI, where all model HTTP happens outside Python's event loop. `anthropic` is not even a dependency. **Reason: wrong layer — there is no `AsyncAnthropic` call site; concurrency is delivered by `ThreadPoolExecutor` over subprocesses (`parallel.py`).** (<https://platform.claude.com/docs/en/cli-sdks-libraries/sdks/python>)

- **`max_tokens:0` cache pre-warm.** Incompatible with extended thinking (`effort=high`), streaming, and structured output — all of which the agentic lane and the judge use — and there is no raw-Messages-API seam to inject it. It also moves rather than removes the cold write, so it is no cheaper than using the first real scenario as the pilot. **Reason: incompatible request shape + no call site; the real-scenario pilot dominates it.** (<https://platform.claude.com/docs/en/build-with-claude/prompt-caching>)

- **Tool Search tool / deferred tool loading.** Solves large tool catalogs (>30–50 tools). The runner exposes a small, per-scenario-restricted allowlist (`toolset.py`); ToolSearch is *deliberately denylisted* to prevent tool-hunting spiral that would burn `max_turns`. **Reason: solves a problem the runner doesn't have, and conflicts with the anti-spiral toolset restriction.** (<https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool>)

- **Fine-grained tool streaming (`eager_input_streaming`), strict tool schema, parallel-tool flags, tool-result minimization.** These are properties of *caller-defined* tools; teatree's tools are CLI built-ins it cannot annotate, and fine-grained streaming risks partial/invalid JSON in the very trajectory-capture path grading depends on. **Reason: not teatree's surface (CLI owns it); zero or negative value.**

- **`inference_geo` / data-residency 1.1× multiplier, `anthropic-ratelimit-*` headers, `with_raw_response`, manual `fallback_credit_token`, server-side `fallbacks`.** All are raw-Messages-API features reachable only with `ANTHROPIC_API_KEY` and a direct client. On the subscription OAuth path (`auth.py:26`) they are unreachable; the billed `total_cost_usd` already reflects whatever geo/fallback the API charged. **Reason: API-key-only + the runner uses subscription OAuth, not the API key the brief assumed.** (The structured `RateLimitEvent`/`api_error_status` the SDK *does* surface is the in-architecture substitute — see #4.15.)

- **Adaptive-thinking manual `budget_tokens` / promptable thinking suppression.** Manual `budget_tokens` 400s on Opus 4.8/4.7; prompt-based thinking suppression would bias the eval away from production behavior (the eval *is* the measurement instrument, not a place to apply the steering). **Reason: dead-end on current models / corrupts the measurement.**

---

## 6. Phased implementation roadmap

Each phase is independently shippable and ordered by value-per-risk. Per CLAUDE.md, every behavioral change ships with a TDD regression test (red before, green after).

### Phase 0 — Premise correction + docs (½ day, zero risk)

- Record in the eval README/notes that the metered lane uses **subscription OAuth** (`auth.py:26`), not an API key, and that per-request `cache_control`/`ttl`/`inference_geo`/Batch/`service_tier`/rate-headers are therefore out of reach via the SDK→CLI transport.
- Note that `excludeDynamicSections`, token-efficient-tools, and interleaved thinking are correctly absent (custom string prompt / built-in / auto-on) so a future contributor doesn't "fix" them.
- Add `CACHE_WRITE_MULTIPLIER_1H = 2.0` to `pricing.py` as a latent correctness guard (no behavior change until a 1h write ever appears).

### Phase 1 — Reliability plumbing (the enabler, low–med effort)

1. **#4.4** Capture `session_id`/`stop_reason`/`api_error_status` (+ `num_turns`, per-step usage) through `message_mapping.py`; add fields to `EvalRun`; pin in the conformance test (`transcript_conformance.py`).
2. **#4.6** Add `model_context_window_exceeded` to a shared dirty/cap constant; add to `CAP_TERMINAL_REASONS`; route to its own report bucket.
3. **#4.5** Branch `extract_terminal_reason` on `stop_reason=='refusal'`; add `EvalSpec.refusal_policy`.
4. **#4.1** Error-type-aware retry: read `api_error_status`, split infra-backoff vs config-fast-fail in `_resilient_matrix_trial`.

*Ships:* flaky/RED trials become replayable and correctly classified; transient overloads stop reading the suite; a wrong key fails fast.

### Phase 2 — Judge quality + cost (low effort)

1. **#4.2** Reasoning-first verdict schema + grade-then-decide prompt.
2. **#4.3** Default judge to a distinct cheaper tier at `effort="low"`; add `JudgeSpec.effort`; gate behind a judge-agreement check on the existing corpus.
3. **#19** Wire (or delete) `JudgeSpec.max_output_tokens`; raise above 512 so the CoT+verdict never truncates.

*Ships:* higher judge accuracy at lower judge cost, with bias mitigation.

### Phase 3 — Throughput + gate fidelity (med effort)

1. **#4.7** Thread `--parallel` into `run_pass_at_k_lane`/`collect_matrix_rows`; size the pool to host + window.
2. **#4.15** Consume `RateLimitEvent`/`api_error_status` in `_collect`; drain (not halt) the pool on `allowed_warning`/429, resume at `resets_at`.
3. **#4.9** Add a significance margin (or Wilson/Fisher) + min-k to the score-regression gate.
4. **#8** Flakiness label when `0<passes<k`, surfaced + persisted.

*Ships:* multi-hour sweeps drop to tens of minutes without 429 storms; the regression gate stops false-reding merges on noise.

### Phase 4 — Cost trimming + observability (low effort)

1. **#11** Backfill `agent_sections` on specs still resending whole SKILL.md (compounds with any caching).
2. **#4.8 / #12** Per-scenario effort tiering driven by captured `thinking_tokens`.
3. **#14** Cache-health advisory: warn on low suite `cache_hit_rate` (note cold-write% is dominated by per-process cold starts — expected, not a bug).
4. **#13** Surface per-turn cost + `num_turns` ("turns used / budgeted") as diagnostics.

*Ships:* lower cold-write mass and output-token spend, with a tripwire so a cache-key regression is visible.

### Deferred (do not build now)

- **1-hour cache TTL (#17)** and **cross-scenario warm-prefix / pilot-then-fan-out (#18)**: both require a per-block `cache_control`/`ttl` knob the SDK does not expose and a byte-stable cross-scenario prefix (blocked today by per-scenario system + per-scenario `--tools` divergence — `toolset.py`). Revisit only if a direct-Messages-API lane is introduced or the CLI surfaces a cache-TTL flag. The `warm_equivalent_cost` diagnostic (`cost_fit.py:93`) already quantifies the headroom, so the business case stays measurable without building anything speculative.
