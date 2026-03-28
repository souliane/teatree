#!/usr/bin/env bash
# Generate full t3 CLI reference from --help introspection.
# Output: docs/cli-reference.md

set -euo pipefail

OUTPUT="docs/cli-reference.md"

echo "# t3 CLI Reference" > "$OUTPUT"
echo "" >> "$OUTPUT"
echo "_Auto-generated from \`t3 --help\` introspection. Do not edit manually._" >> "$OUTPUT"
echo "" >> "$OUTPUT"

# Top-level help
echo "## t3" >> "$OUTPUT"
echo '```' >> "$OUTPUT"
t3 --help >> "$OUTPUT" 2>&1 || true
echo '```' >> "$OUTPUT"
echo "" >> "$OUTPUT"

# Walk subcommands
for cmd in $(t3 --help 2>&1 | grep -E '^\s+\w' | awk '{print $1}'); do
    echo "## t3 $cmd" >> "$OUTPUT"
    echo '```' >> "$OUTPUT"
    t3 "$cmd" --help >> "$OUTPUT" 2>&1 || true
    echo '```' >> "$OUTPUT"
    echo "" >> "$OUTPUT"
done

echo "Generated: $OUTPUT"
