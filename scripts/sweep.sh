#!/usr/bin/env bash
# Sensitive-data sweep — run before every push (see CLAUDE.md).
#
# Patterns live in .sweep-patterns.local (UNTRACKED, gitignored) so the
# banned strings themselves never appear in the repo. Previously the sweep
# instruction inlined its grep pattern in CLAUDE.md, which published the very
# strings it was guarding against (ADR-023). Copy .sweep-patterns.example to
# .sweep-patterns.local and fill in real patterns, one extended regex per
# line; lines starting with # are comments.
#
# Exit 0 = clean, 1 = matches found (printed), 2 = patterns file missing.
set -euo pipefail
cd "$(dirname "$0")/.."

PATTERNS_FILE=".sweep-patterns.local"
if [[ ! -f "$PATTERNS_FILE" ]]; then
    echo "sweep: $PATTERNS_FILE not found — copy .sweep-patterns.example and fill in real patterns." >&2
    exit 2
fi

# Build one alternation from non-comment, non-empty lines.
PATTERN="$(grep -vE '^\s*(#|$)' "$PATTERNS_FILE" | paste -sd'|' -)"
if [[ -z "$PATTERN" ]]; then
    echo "sweep: $PATTERNS_FILE has no patterns." >&2
    exit 2
fi

# Sweep everything git would ship (tracked files), excluding this mechanism.
MATCHES="$(git ls-files -z \
    | grep -zv '^\.sweep-patterns\.example$' \
    | xargs -0 grep -rniE "$PATTERN" -- 2>/dev/null || true)"

if [[ -n "$MATCHES" ]]; then
    echo "sweep: BANNED STRINGS FOUND — do not push:" >&2
    echo "$MATCHES" >&2
    exit 1
fi
echo "sweep: clean ($(git ls-files | wc -l | tr -d ' ') tracked files)"
