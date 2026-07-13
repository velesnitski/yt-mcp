# 030 — Automated ticket-ID / project-code guard in `sweep.sh`

## Context

The sensitive-data sweep (ADR-023) caught product/brand strings but not real
YouTrack **project-code / ticket-ID prefixes** — which re-leaked into tests
and docstrings four separate times (latest 2026-07-10). Live-testing against
the real instance keeps seeding real IDs into fixtures. A plain sweep rule
can't cover them cleanly:

- The real prefixes must **not** be hardcoded in the tracked script (that was
  the ADR-023 lesson: the guard would become the leak).
- Historical mentions in `docs/adr/*` are **grandfathered** (owner decision) —
  a global rule would flag them and break `release.sh prepare` forever.
- The prefixes are uppercase, so case-insensitive matching false-positives on
  lowercase words that share a prefix.

## Decision

`sweep.sh` gains a second pattern scope, keyed by a `NOADR:` line prefix in the
untracked `.sweep-patterns.local`:

- **plain lines** → every tracked file, case-insensitive (product/brand/host).
- **`NOADR:` lines** → every tracked file **except** `docs/adr/*` and
  `CHANGELOG*`, **case-sensitive**. Real project-code / ticket-ID prefixes go
  here.

The real prefixes live only in the untracked patterns file — never in the
repo. `.sweep-patterns.example` documents the `NOADR:` convention with
placeholder prefixes only.

## Consequences

- New code/tests can no longer reintroduce a real ticket ID without failing
  `scripts/sweep.sh` (which runs in `release.sh prepare`). Self-tested: it
  catches a planted ID in `src/`, stays clean on the current tree, and
  correctly ignores the grandfathered ADR mentions.
- Historical ADRs are untouched; their mentions don't break the gate.
- Tooling-only; no runtime or package change.
