#!/usr/bin/env bash

# SessionStart hook: ensure the t3 CLI is available.
# Checks PATH first, then uv project context. Does NOT auto-install
# from PyPI since teatree is not published yet.

if command -v t3 >/dev/null 2>&1; then
    t3 doctor check 2>/dev/null || true
fi
