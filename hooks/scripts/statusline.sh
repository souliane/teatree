#!/usr/bin/env bash
# Thin statusline hook: cat the file the loop has rendered.
# The loop (`teatree.loop.statusline.render`) writes the file; this hook
# only reads it. Decoupling render from read keeps the hook fast (<10ms)
# regardless of how much work the tick does composing the content.

set -u

target="${TEATREE_STATUSLINE_FILE:-${XDG_DATA_HOME:-$HOME/.local/share}/teatree/statusline.txt}"

if [[ -r "$target" ]]; then
    cat "$target"
fi
