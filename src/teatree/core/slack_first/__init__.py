"""The Slack-first relay's owner transport: who the owner is, whether the listener is alive, and the post itself.

An attended session relays its owner prompts through the factory's Slack DM while that integration is live.
The leaves here are the pieces every relay surface — the CLI, the hooks, the doctor and the catch-up job — reads
the same way, so none of them judges liveness or the owner id on its own.
"""
