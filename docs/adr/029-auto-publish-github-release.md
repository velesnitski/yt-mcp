# 029 — Auto-publish a GitHub Release in `release.sh ship`

## Context

`release.sh ship` (ADR-025) pushes a git **tag** but never created a GitHub
**Release** object. Result: the Releases page showed **v1.7.0** (April) as
"Latest" for months while the real latest was v1.18.2 — every version since
existed only as a tag. Releases (not tags) are what the GitHub UI surfaces,
what some watchers/scanners key off, and where the security release (v1.18.1)
should be visible. v1.18.0–v1.18.2 were backfilled by hand; the gap would
recur every ship without automation.

## Decision

`ship` now publishes a GitHub Release for the tag, right after the tag push:

```sh
gh release create "$tag" --title "$tag" --generate-notes --latest
```

- `--generate-notes` writes notes from the commits since the last release
  (editable afterward); `--latest` moves the "Latest" badge onto the new tag.
- **Non-fatal by design:** a successful `git push` must never be undone by a
  Release hiccup. The step is guarded — skips if `gh` is absent (warn), skips
  if the release already exists (re-ship), and warns instead of aborting on
  any `gh release create` failure. `set -euo pipefail` stays satisfied.

## Consequences

- Every future `ship` leaves the Releases page truthful, with the newest
  version marked Latest — no manual step, no drift.
- Pre-tagging history (v1.7.1–v1.17.3) has no tags and is deliberately not
  backfilled (low value).
- Tooling-only change; the shipped package is byte-identical to v1.18.2.
  Patch bump 1.18.2 → 1.18.3 keeps the release cadence uniform and dogfoods
  the new step (this very release is the first auto-published one).
