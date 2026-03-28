# Investigation: Simulating Claude SDK with Team Plan

**Status:** Open
**Issue:** #5

## Questions

1. Can the Claude SDK be pointed at a local proxy or mock server?
2. Does the Team plan have any sandbox/test mode?
3. Could we record and replay SDK interactions for deterministic testing?
4. What's the cost model for headless agent runs on Team vs API plans?

## Approach Options

- **VCR.py / cassettes**: Record real API interactions, replay in CI
- **Lightweight response stubs**: Mock HTTP client with canned responses
- **Contract tests**: Verify adapter satisfies protocol without real API
- **Hybrid**: Contract tests + small VCR set for smoke testing

## Recommendation

_Pending investigation._
