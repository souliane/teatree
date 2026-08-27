# Evidence capture — screenshot and video recipes

The recipes behind `/t3:e2e` § "Screenshot Sanity Check" and § "Video Sanity Check". Those sections carry the evidence bar; this file carries the Playwright settle and red-box code, the pixel-count gate details, and the ffprobe pre-roll check.

## Settle, red-box, and the pixel-count gate

```ts
await expect(page.locator('[data-test=expected-element]')).toBeVisible();
await page.waitForLoadState('networkidle');
await page.screenshot({ path: `${process.env.T3_E2E_ARTIFACTS_DIR}/<TICKET>/<env>/step1.png` });
```

**Red-box the asserted element in DEV captures (evidence, not decoration).** A screenshot posted as evidence must make the asserted element obvious, not leave a reviewer hunting a full page for it. Before the capture, draw a saturated-red box around the element under assertion (a bright `outline`/`border` injected via `element.evaluate(...)`, or a Playwright highlight) so the captured PNG carries an unmissable marker on exactly the field/control the test verifies. This is the same red-box marker the write-test-plan evidence gate looks for in DEV captures — a deployed-env screenshot whose asserted element isn't visibly boxed reads as a generic page shot, not proof the specific behaviour rendered.

```ts
const el = page.getByLabel('Default purchase costs');
await expect(el).toBeVisible();
await el.evaluate((n) => { n.style.outline = '4px solid #ff0000'; n.style.outlineOffset = '2px'; });
await el.scrollIntoViewIfNeeded();
await page.screenshot({ path: `${process.env.T3_E2E_ARTIFACTS_DIR}/<TICKET>/dev/step1.png` });
```

Capture the red-boxed shot only after the settle (visible + network idle) above — a red box around a not-yet-rendered element is no more evidence than a blank page.

**The red-box gate is a real pixel-count check — self-check before posting, not after a rejected post.** `t3 <overlay> e2e write-test-plan` rejects a `capture-matrix` image whose saturated-red pixel count is below the gate's minimum (`teatree.core.evidence.test_plan_validation._saturated_red_pixel_count`; a real highlighted crop clears it comfortably while a hidden-state / absence shot or a pre-red-box capture reads ~0 and is rejected). Two adjacent gates trip silently if you assemble carelessly:

- **A hidden-state or "element absent" screenshot has no red box to draw, so it scores ~0 and is rejected.** If an AC's evidence is the *absence* of an element, that AC's modality is `link-api` (a status/redirect), not a boxed screenshot — don't try to post an unboxed shot.
- **Byte-identical-image dedup (md5):** no two byte-identical images may appear in one manifest, and a `dev/` and `local/` capture of the same state can come out byte-identical — include each distinct state once, or ensure the two sides' bytes actually differ.
- **A re-run replaces a whole side, not one workflow:** supplying a `dev` (or `local`) block replaces that ENTIRE side — commits, `missing_on_dev`, and ALL its workflows. To update one workflow on a side, re-send every workflow for that side or the others vanish (workflows are keyed by name; `steps` persist across runs).

On the slower DEV stack the injected red box can render as ~0 px for a visible-element assertion even when the element is on screen (a timing/scroll/crop race the local stack doesn't hit). Mitigate by `scrollIntoViewIfNeeded()`, waiting for the outline to actually apply, and cropping so the outline is inside the captured region — before the screenshot, not after.

## Verifying a recording's pre-roll

The deterministic check is `teatree.core.evidence.video_evidence` (mirroring `teatree.core.evidence.test_plan_validation` for stills) — it shells ffprobe/ffmpeg to measure the leading blank/static run and refuses an over-budget pre-roll. Run it directly on any recording before posting:

```bash
# Verify one recording (exits non-zero on excessive blank/static pre-roll):
uv run python scripts/analyze_video.py "$T3_E2E_ARTIFACTS_DIR/<TICKET>/local/run.webm" --verify
```

This check is **machine-enforced by `write-test-plan`**: `t3 <overlay> e2e write-test-plan` runs `check_video_evidence` over every manifest `video` alongside the image gates and **refuses the write** (naming the dead-lead seconds) when a recording opens with excessive pre-roll — so a dead-lead video can never be cited by a plan. When ffmpeg is absent the check skips cleanly (it never blocks a write merely because the host lacks ffmpeg); `--skip-validation` is the user-authorised bypass (the agent never sets it itself). The final-frame clarity is the author's discipline — capture so the recording holds the asserted end-state, then `--verify` the head.
