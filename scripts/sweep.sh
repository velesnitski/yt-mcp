#!/usr/bin/env bash
# Sensitive-data sweep — run before every push (see CLAUDE.md).
#
# Patterns live in .sweep-patterns.local (UNTRACKED, gitignored) so the banned
# strings themselves never appear in the repo. Two scopes, both over EVERY
# tracked file:
#   * plain lines      → matched case-INSENSITIVE (product/brand/host strings).
#   * lines with CS:   → matched case-SENSITIVE (project-code / ticket-ID
#                        prefixes are uppercase; case-sensitive avoids false
#                        hits on lowercase words that share a prefix).
#
# Copy .sweep-patterns.example to .sweep-patterns.local and fill in real
# patterns (one extended regex per line; # comments and blank lines ignored).
#
# Exit 0 = clean, 1 = matches found (printed), 2 = patterns file missing.
set -euo pipefail
cd "$(dirname "$0")/.."

PATTERNS_FILE=".sweep-patterns.local"
if [[ ! -f "$PATTERNS_FILE" ]]; then
    echo "sweep: $PATTERNS_FILE not found — copy .sweep-patterns.example and fill in real patterns." >&2
    exit 2
fi

CI_PATTERN="$(grep -vE '^\s*(#|$)' "$PATTERNS_FILE" | grep -vE '^CS:' | paste -sd'|' - || true)"
CS_PATTERN="$(grep -E '^CS:' "$PATTERNS_FILE" | sed 's/^CS://' | paste -sd'|' - || true)"

if [[ -z "$CI_PATTERN" && -z "$CS_PATTERN" ]]; then
    echo "sweep: $PATTERNS_FILE has no patterns." >&2
    exit 2
fi

files() { git ls-files -z | grep -zv '^\.sweep-patterns\.example$'; }
fail=0

if [[ -n "$CI_PATTERN" ]]; then
    M="$(files | xargs -0 grep -rniE "$CI_PATTERN" -- 2>/dev/null || true)"
    if [[ -n "$M" ]]; then
        echo "sweep: BANNED STRINGS FOUND — do not push:" >&2
        echo "$M" >&2
        fail=1
    fi
fi

if [[ -n "$CS_PATTERN" ]]; then
    M="$(files | xargs -0 grep -rnE "$CS_PATTERN" -- 2>/dev/null || true)"
    if [[ -n "$M" ]]; then
        echo "sweep: TICKET-ID / PROJECT-CODE LEAK — do not push:" >&2
        echo "$M" >&2
        fail=1
    fi
fi

[[ "$fail" == 0 ]] || exit 1
echo "sweep: clean ($(git ls-files | wc -l | tr -d ' ') tracked files)"
