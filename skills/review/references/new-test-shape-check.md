# New-test shape check

## New-Test Shape Check (Non-Negotiable)

When the diff adds or modifies test files, verify the new tests follow the repo's test-writing doctrine (see the repo's `AGENTS.md` § "Test-Writing Doctrine" — teatree and every overlay repo carry the same rule):

1. **Mock density.** If a new test file is mostly `Mock()`, `patch()`, `MagicMock`, or `mock.call_args` assertions, flag it. Ask: could this have been a Django test client call, a `call_command` invocation, a real `tmp_path` git repo, or a Playwright E2E?
2. **Mock targets.** Mocks should hit unstoppable externals only — network (GitHub, GitLab, Slack, Sentry), clock, `pass`, third-party subprocesses. Mocking teatree code, Django models, filesystem under `tmp_path`, or `git` itself is a finding.
3. **Missing integration coverage.** If the diff adds a view, a management command, or a new CLI surface and only ships unit tests, flag it — the happy path belongs in an integration test.
4. **Coverage preservation.** Any test rebalancing (removing units, adding integration) must keep the coverage gate satisfied. Report the before/after coverage number in the review.

Accept a mock-heavy test only when the PR description justifies why a higher-level test couldn't cover the same behavior (e.g., a rare error branch that's painful to trigger through the real entry point).
