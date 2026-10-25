# Review NIT lane

On a colleague's MR, post each BLOCKING finding inline and withhold approval. For a NIT-only review, post each NIT inline, then approve with `t3 review approve`. Create the follow-up issue afterward; issue tracking never gates approval.

On an owner-authored PR, fix BLOCKING findings directly, commit and push, then summarize the fixes to the owner. A NIT-only review does not return `hold`: post each NIT inline.

BLOCKING covers wrong behaviour, false claims, untested shipped behaviour, vacuous or redundant tests, duplicate definitions, and red checks. NIT covers wording, naming, style, and placement.

For each NIT-only review, track the set in one follow-up issue labelled `nit` and `t3-auto`. Read the issue back before resolving its inline threads. On a NIT-ticket PR, treat every finding as blocking so the same work cannot be deferred again.
