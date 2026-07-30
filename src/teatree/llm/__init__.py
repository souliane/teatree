"""Provider-neutral LLM plumbing.

Currently houses the credential layer (:mod:`teatree.llm.credentials`) — the one
canonical way to authenticate any Claude SDK / bundled-CLI invocation in teatree.
It is designed provider-neutral so it later becomes an ``LLMBackend.credential``
(Claude today, other providers later) with no rework at the call sites.
"""
