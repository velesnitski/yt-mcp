#!/usr/bin/env bash
# Sensitive-data sweep — run before every push (see CLAUDE.md).
#
# Patterns live in .sweep-patterns.local (UNTRACKED, gitignored) so the banned
# strings themselves never appear in the repo (ADR-023). Two scopes:
#   * plain lines          → matched (case-insensitive) against EVERY tracked
#                            file. Use for product/brand/host strings.
#   * lines prefixed NOADR: → matched (case-SENSITIVE) against every tracked
#                            file EXCEPT docs/adr/* and CHANGELOG* (ADR-030).
#                            Use for real project-code / ticket-ID prefixes,
#                            whose historical mentions in ADRs are
#                            grandfathered; case-sensitive avoids false hits on
#                            lowercase words that share a prefix.
#
# Copy .sweep-patterns.example to .sweep-patterns.local and fill in real
# patterns (one extended regex per line; # comments, blank lines ignored).
#
# Exit 0 = clean, 1 = matches found (printed), 2 = patterns file missing.
set -euo pipefail
cd "$(dirname "$0")/.."

PATTERNS_FILE=".sweep-patterns.local"
if [[ ! -f "$PATTERNS_FILE" ]]; then
    echo "sweep: $PATTERNS_FILE not found — copy .sweep-patterns.example and fill in real patterns." >&2
    exit 2
fi

ALL_PATTERN="$(grep -vE '^\s*(#|$)' "$PATTERNS_FILE" | grep -vE '^NOADR:' | paste -sd'|' - || true)"
NOADR_PATTERN="$(grep -E '^NOADR:' "$PATTERNS_FILE" | sed 's/^NOADR://' | paste -sd'|' - || true)"

if [[ -z "$ALL_PATTERN" && -z "$NOADR_PATTERN" ]]; then
    echo "sweep: $PATTERNS_FILE has no patterns." >&2
    exit 2
fi

fail=0

# Scope 1: product/brand strings — every tracked file, case-insensitive.
if [[ -n "$ALL_PATTERN" ]]; then
    M="$(git ls-files -z \
        | grep -zv '^\.sweep-patterns\.example$' \
        | xargs -0 grep -rniE "$ALL_PATTERN" -- 2>/dev/null || true)"
    if [[ -n "$M" ]]; then
        echo "sweep: BANNED STRINGS FOUND — do not push:" >&2
        echo "$M" >&2
        fail=1
    fi
fi

# Scope 2: ticket-ID / project-code prefixes — tracked files EXCEPT docs/adr
# and CHANGELOG (grandfathered), case-sensitive.
if [[ -n "$NOADR_PATTERN" ]]; then
    M="$(git ls-files -z \
        | grep -zvE '^(docs/adr/|CHANGELOG|\.sweep-patterns\.example$)' \
        | xargs -0 grep -rnE "$NOADR_PATTERN" -- 2>/dev/null || true)"
    if [[ -n "$M" ]]; then
        echo "sweep: TICKET-ID / PROJECT-CODE LEAK (excl. docs/adr) — do not push:" >&2
        echo "$M" >&2
        fail=1
    fi
fi

[[ "$fail" == 0 ]] || exit 1
echo "sweep: clean ($(git ls-files | wc -l | tr -d ' ') tracked files)"
