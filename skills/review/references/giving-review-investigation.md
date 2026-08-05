# Giving review — the pre-posting investigation steps

The step-by-step procedure behind `/t3:review` § "Giving Code Review". That section carries the verdict rules; this file carries Steps 0 through 0h — the investigation every finding clears before it is drafted.

1. Extract the ticket URL or number from the PR title/description.
2. Fetch the issue via the `mcp__teatree__github_issue` / `mcp__teatree__gitlab_issue` MCP tool (structured JSON; fall back to the issue tracker CLI — `glab issue view`, `gh issue view` — when the MCP server isn't connected).
3. **Fetch every attached spec** (PDFs, OpenAPI files, vendor docs) and every linked external requirement. For GitLab attachments, the working path is `glab api projects/<id>/uploads/<secret>/<filename>` — browser-style URLs (`gitlab.com/<group>/<repo>/uploads/...`, `gitlab.com/-/project/<id>/uploads/...`) require session cookies and return login HTML when hit with a PAT. Attachments are the authoritative spec; an author docstring summarising them is not a substitute.

   A posted image or screenshot is two layers, and a fetched image is read in two passes. The first pass is the raw capture — what the tool, page, or table actually shows. The second pass is the poster's overlay drawn on top: borders and boxes, arrows, circles, highlights and colour, underlines, callout text, numbering, redaction. Those marks are a deliberate hint pointing at exactly what the poster wants seen; reading only the raw content answers a question the poster did not ask. For each annotation, ask "why did they mark exactly this" and resolve it concretely: a boxed cell is the load-bearing value the argument turns on; an arrow is an A→B link being asserted; bordered individual letters spell an acronym — decode it; a circled token is the disputed item; added callout text is the poster's claim restated. When an annotation's meaning is non-obvious, decoding it is required investigation, not optional — an unresolved mark is an unread part of the spec, treated the same as an attachment that did not download.
4. If external requirements links are referenced, fetch those too.
5. Use the ticket context + attachments as the ground truth for evaluating correctness.

**Hard rule — refuse blind reviews.** If a ticket references a spec attachment or external requirements document that you cannot retrieve, **STOP**. Do not post review notes. Report back to the user: which document you couldn't fetch, what you tried, and what permission / access / exception is needed. Overlay skills MAY declare specific sources as out-of-scope (partner portals behind SSO the sandbox cannot reach, for example); honour those per-overlay exceptions. For anything else, a review with missing spec context is not a review — it's guessing, and guessing attached to the user's account damages the author's trust.

Without ticket context you cannot judge whether the implementation is correct — only whether it compiles.

**Step 0b — Review All Commits, Not Just the Final Diff:**

The combined diff can hide mistakes. Always check individual commits:

1. List all PR commits (e.g., `glab api .../merge_requests/<IID>/commits`).
2. Inspect each commit's diff individually — a later commit may accidentally revert an earlier fix.
3. Look for "Tests fix" / "Fix tests" follow-up commits that change production code alongside test adjustments.

**Step 0c — Discuss Before Posting:**

Present ALL findings to the user before posting any comments. Never silently drop findings between the discussion phase and the posting phase — if a finding was discussed, it gets posted unless the user explicitly removes it. The user curates; you surface.

When raising concerns about caching, stale data, or side effects: **investigate first**. Check the actual code paths and real data before speculating. A concern backed by evidence ("I checked the DB — durations do vary") is useful; a speculative "this might be a problem" wastes the author's time.

**Step 0d — Answer Your Own Questions Before Posting (Non-Negotiable):**

Every review comment is posted under the user's name. A comment that boils down to "I'm unsure, please confirm" makes the *user* look like they don't know their own codebase. Do not post it.

Before drafting any comment, if it would contain any of the following phrases — or their equivalents — **STOP and investigate first**:

- "worth confirming with the business that…"
- "worth checking `<file>` / `<function>` / the downstream serializer / etc."
- "can you confirm this value matches what upstream emits?"
- "is this string / identifier / enum value correct?"
- "does this field exist in the producer schema?"
- "I'm not sure whether…"
- "does this mean that… / or… / or…?" (listing options instead of picking one)
- "verify that …" / "please check …" / "confirm whether …" — any imperative that asks the author to do verification work the reviewer is capable of doing themselves.

**The reviewer does the verification, not the author.** If the comment names a file, function, schema, enum, downstream caller, or any other artifact reachable from the local checkout, **open it and read it before posting**. "Worth checking `foo.py`" is not a review comment — it is the reviewer outsourcing their job. Either the file says the code is wrong (post a verified finding) or it says the code is fine (post nothing).

Investigate first by exhausting the sources you **can** reach:

1. **Grep the repo** for the symbol / string / identifier — producers, consumers, enums, tests, fixtures, docs.
2. **Grep sibling repos** when the value crosses a service boundary (e.g., webhook producer → consumer, API schema → client). The upstream producer's source of truth lives there. Discover sibling repos via `T3_WORKSPACE_DIR` or the overlay's configured repo list — never hardcode a user-specific path.
3. **Read the producer's schema / enum / migration** — whichever repo emits the value. If it's a Django model, check the field's `choices=` and the migration history. If it's a Pydantic model, check the field type.
4. **Check commit history** for the rename, addition, or removal — `git log -S "<symbol>" --all --oneline` often shows exactly when and why the value changed.
5. **Read the test fixtures** — realistic test inputs show what the producer actually sends.
6. **Check related PRs** on the same or upstream repos for the same symbol — someone may have already merged or discussed it.

Only after all reachable sources are exhausted can you post a question-style comment — and only when the answer truly requires access you do not have (partner portal behind SSO, vendor-only documentation, product owner's desk knowledge). State what you checked and why the answer isn't reachable, so the author sees you did the work.

**Scale severity to confidence — on a colleague-facing post, drop what stays speculative.** A speculative "maybe wrong?" is a nit at best; do not post it under the owner's name. A verified finding ("grepped `foo-producer`, canonical spelling is `X`, branch has `Y` — will fail at runtime") is a blocker and belongs in the review. In the **verdict envelope** the disposal is the opposite: record the uncertain observation with its confidence rather than dropping it.

**When the investigation confirms the code is correct, post nothing — on a colleague-facing post.** Silence on a check you performed is the correct outcome there, not a "looks good, but…" comment. Positive comments belong in the summary to the user, not in the PR. In the **verdict envelope** that same clean check is *recorded*, not silenced: name what you checked and that it came back clean.

**Step 0e — Don't Police Other Authors' Title/Description Format (Non-Negotiable):**

Do NOT leave review comments about an external author's PR title format, description wording, commit-message style, work-item link spacing, or whether their description "reads better" in a different shape. These rules are enforced by CI and by the overlay's `validate_pr()` check — not by the reviewer. Raising them manually duplicates the bot and nags a colleague for something a machine already polices.

The reviewer's responsibility is to ensure **their own** PRs pass the title/description check. On other authors' PRs, silence on formatting is the correct outcome. If something is objectively wrong in a way that affects traceability or release notes (e.g., the title references the wrong ticket), frame it as a **correctness** finding, not as a style nit.

**Step 0f — Respect the Overlay's Auto-Close Policy (Non-Negotiable):**

Do NOT suggest adding `Closes #NNN`, `Fixes #NNN`, `Resolves #NNN`, or any other auto-close keyword to a PR description unless the active overlay's conventions explicitly require it. Many overlays manage issue closure via their own ticket/PR linking rather than via GitHub-style auto-close trailers, and suggesting them contradicts the overlay convention.

Check the overlay skill's commit-message and PR-description rules **before** proposing any trailer. The default when the overlay is silent on the topic is: do not suggest auto-close trailers.

**Step 0g — Cross-Service Verification (Non-Negotiable):**

A review of a service that talks to other services is incomplete until those other services have been checked. Reviewing one repo in isolation produces blind comments — the reviewer asserts "this is the convention" or "this default is fine" without knowing what the producers and consumers across the platform actually do. Comments built on that premise undermine the reviewer's credibility when the author replies with "have you checked the FE / the gateway / the sibling microservice?".

**Before posting any comment about a name, contract, default value, schema field, response shape, or wire format, exhaust the cross-service grep:**

1. **Enumerate the related services** at the start of the review. From the PR's repo, list every service that produces or consumes the same data: upstream gateway, downstream consumers (frontend, sibling backend, document generation, data warehouse), shared schema/proto repos. Discover them via `T3_WORKSPACE_DIR` (or the overlay's configured repo list) — never hardcode a user-specific path. State the list explicitly so the user can correct gaps before you start.
2. **Grep every related service** for the symbol/string/identifier/field name. Frontend models, backend serializers, fixture files, generated docs, OpenAPI specs, migration histories. Note where each occurrence lives.
3. **Cite the cross-service evidence in the comment.** "Frontend has 18 references to `idExpirationDate` in `libs/shared/data-model/...`; the gateway-side Python repo has matching references in `report-generator/serializers/...`" is a finding. "I think this should be spelled differently" is a guess.
4. **When the cross-service check reveals the comment was wrong, drop the comment.** A comment that survives the check survives because the platform-wide convention contradicts the diff. Silence is the correct outcome on a check that confirmed the diff is fine.
5. **When the cross-service check is impossible** (a repo is not in scope, sandboxed, or behind access you don't have), say so explicitly in the comment and name what was checked vs not. Don't pretend you ran a check you didn't.

**Triggers for this step** — every diff touching:

- A schema field name, enum value, or wire format (Pydantic models, DRF serializers, TypeScript interfaces, OpenAPI definitions).
- A default value or boolean flag that previously had a different default (especially flags with always-on/always-off semantics like loyalty enrichment, feature gates, search filters).
- A response shape or wrapper type returned by an endpoint already consumed by another service.
- A query parameter name or required/optional toggle on a public endpoint.
- A renamed function, method, or class that is used cross-repo (gateway client, shared library, public CLI command).

If none of those triggers apply (purely internal refactor, test-only change, comment fix), this step is satisfied automatically.

**Failure mode this step prevents:** a reviewer posts "the canonical name should be X" based on the local repo's pattern, the author replies "the FE has 20 usages of Y, please check before commenting", and the user (whose name is on the comment) loses credibility for a finding that would have been correct if the reviewer had grepped the FE first.

**Step 0h — Discussion Venue: PR Over Chat (Non-Negotiable):**

Discussion topics that anchor to specific code in a PR — design questions about a function, a TODO in the diff, a missing call compared to a sibling endpoint, a hardcoded value, an architectural choice visible in the patch — belong on the **PR**, not in a Slack/Teams DM or chat thread. Default to PR notes whenever the topic references something the reviewer can point to in the diff.

**Why PR over chat:**

- PR notes are persistent, threaded per topic, and resolve with the PR. Chat scrolls away.
- Other reviewers and stakeholders see PR notes; chat is a private channel between two people.
- PR notes attach to the line/file, so the conversation stays anchored to the code that triggered it.
- The ticket's audit trail benefits from the discussion living next to the code change.

**When chat is the right venue:**

- A heads-up that the review is ready and points to the PR for the discussion ("left some thoughts on !351").
- Coordination/scheduling ("can we pair on the LE flow tomorrow?").
- Sensitive feedback that doesn't belong in a public review trail.
- Topics genuinely unrelated to the diff (e.g., process discussion about how the team reviews PRs).

**Inline first, general note second:**

When posting on the PR, prefer **inline** (line-anchored) discussions over **general** PR comments. Inline notes show the exact code that triggered the question and let the author resolve them per topic. Use a general PR comment only when the topic is not anchorable to a single line — for example, an architectural question that spans the whole file or a code block that is not part of the PR's diff (so GitLab cannot anchor an inline note to it).

**Failure mode this step prevents:** the reviewer drafts a Slack message containing 5 design questions about specific lines of a PR, sends it as a DM, and the discussion lives in chat where it is invisible to the rest of the team and disconnected from the code. The author then has to copy-paste the chat back into PR comments to track resolution. The right move was to post the topics as PR discussions in the first place and send a one-line Slack heads-up pointing to the PR.
