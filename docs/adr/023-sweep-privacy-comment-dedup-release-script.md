# 023 — Sweep privacy fix, comment dedup, release ceremony script

## Context

A token-effectiveness audit of the operating loop around this repo found
three fixable costs — and, while implementing the first, a real
**sensitive-data leak in the public repo**.

### 1. The sweep instruction WAS the leak

`CLAUDE.md` (tracked, public since 2026-04-15) inlined the pre-push
sensitive-string grep — thereby publishing the exact banned names it was
built to keep out. The guard was the violation. `git log -S` confirms
`CLAUDE.md` is the only tracked file that ever contained them; every commit
since 2026-04-15 carries it, so the string remains in **history** even after
this fix (see Consequences).

### 2. Duplicate bot comments burn tokens on every issue read

Workflow bots post the same nag repeatedly (a recent issue carried 4× "log
your time" + 3× "no movement" — 7 comments, 2 distinct texts). `get_issue`
returned all of them verbatim in both report and JSON output.

### 3. Release ceremony was ~8 manual steps

bump ×2 → sync → tests → sweep → commit → push dev → ff-merge main → push →
verify → uvx refresh → relabel. Six releases this week ≈ 50 tool calls of
pure ceremony.

## Decision

1. **`scripts/sweep.sh` + untracked patterns.** Patterns live in
   `.sweep-patterns.local` (gitignored; `.sweep-patterns.example` documents
   the format with placeholders only). The script sweeps every tracked file
   and exits non-zero on a hit. `CLAUDE.md` now says "run scripts/sweep.sh"
   and explicitly forbids inlining banned strings anywhere tracked. Its
   first run caught the CLAUDE.md leak; the fixed tree sweeps clean. A new
   test (`test_sweep.py`) runs the sweep as a regression guard, auto-skipped
   where the local patterns file doesn't exist (CI/fresh clones).
2. **`dedupe_comments` in formatters.** Comments with identical
   (author, text) collapse to the first occurrence plus a repeat count —
   JSON gains an additive `repeats` field; report/compact renders show
   "(×N)". Distinct texts/authors and empty texts are never merged; input
   list is not mutated. Applied in `normalize_issue` and both
   `format_issue_detail` paths.
3. **`scripts/release.sh`** with two phases: `prepare <version>` (validated
   bump of both version sites, `uv sync`, full test run, sweep) and `ship`
   (push dev, ff-merge main, push, remote-SHA verification, uvx refresh,
   `/mcp` relabel). The commit stays manual — the message needs a brain —
   and `ship` runs only after explicit confirmation, preserving the
   confirm-before-push rule. `prepare` was dogfooded for this very release.

## Consequences

- The banned strings are gone from the working tree but persist in git
  history (every commit 2026-04-15 → now). Full removal requires a
  `git filter-repo` history rewrite + force-push of both branches —
  destructive, owner-confirmed operation; deliberately NOT done here.
- Issue reads with bot spam shrink (the motivating example: 7 comment
  blocks → 2 with ×4/×3 markers) — every consumer benefits, no parameter
  changes needed. Comment count header still shows the true total.
- Releases cost 2 script calls + 1 commit instead of ~10 steps.
- Tests 746 → 752 (dedup unit coverage incl. no-mutation and empty-text
  guards; normalize_issue `repeats` exposure; tracked-files sweep guard).
- Patch bump 1.17.1 → 1.17.2.
