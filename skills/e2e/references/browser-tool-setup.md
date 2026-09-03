# chrome-devtools-mcp — registration and pre-authorization

The setup mechanics behind `/t3:e2e` § "Browser tool: chrome-devtools-mcp (default)". That section carries the headless rule and what the tool is for; this file carries the registration command, the permission entry, and the MCP allow-rule scoping finding.

**Register it (default on):**

```bash
t3 mcp browser-diagnosis   # prints the `claude mcp add` line; the flag ships ON by default
# turn OFF only on a host that cannot run the server:
# t3 <overlay> config_setting set chrome_devtools_mcp_enabled false
```

The registration is `claude mcp add chrome-devtools -- npx -y chrome-devtools-mcp@latest --headless=true`, so the tools surface as `mcp__chrome-devtools__*` — `navigate_page`, `click`, `fill` / `fill_form`, `type_text`, `upload_file`, `wait_for`, `take_snapshot`, `take_screenshot`, `list_console_messages`, `list_network_requests`, `evaluate_script`. Browser-visible breakage (a blank render, a failed XHR, a console error, a wrong DOM state) is diagnosed **in the browser** with these before any root-cause claim, not guessed from the server side.

**Optional aid for authoring/debugging Playwright specs, never required (#3271).** The same live DOM/console/network view makes *writing* a Playwright spec (finding the right selector, confirming the expected DOM state) and *debugging* a red one far more tractable than working blind. It is purely a developer-experience aid — teatree's runtime requires **zero** MCP, so its absence gates nothing: `t3 doctor` only ever emits an INFO suggestion for it, never a WARN/FAIL. Prerequisite: a Chrome/Chromium executable on the host (the server launches its own Chrome over the DevTools Protocol).

**Pre-authorize the tools for an unattended run.** So the tool never prompts mid-run, allow the server in `~/.claude/settings.json`:

```jsonc
{
  "permissions": {
    "allow": [
      "mcp__chrome-devtools__*"
    ]
  }
}
```

**Research finding — MCP allow-rules match by server + tool name only, no domain form.** Per the [Claude Code permission rule syntax](https://docs.claude.com/en/docs/claude-code/permissions#mcp), an MCP specifier is `mcp__server`, `mcp__server__*`, or `mcp__server__tool_name` — there is **no** argument/domain form, so you cannot scope `mcp__chrome-devtools__navigate_page` to a domain the way `WebFetch(domain:example.com)` scopes `WebFetch`. The allow-rule is therefore all-or-nothing per tool. Since chrome-devtools-mcp drives its own launched Chrome (not a shared extension session), there is no per-origin browser gate to clear on top of the allow-rule — allowing the tool is sufficient for an unattended run.
