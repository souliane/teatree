#!/usr/bin/env bash

# SessionStart hook: ensure the t3 CLI is available.

if command -v t3 >/dev/null 2>&1; then
    t3 doctor check 2>/dev/null || true
fi
