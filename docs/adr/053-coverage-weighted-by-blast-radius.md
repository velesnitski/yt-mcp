# 053 — Coverage weighted by blast radius

## Context

Measured coverage for the first time: 56% of statements, 239 partial
branches. The instinct is to raise the number. The evidence from this
month says the number is not the thing.

Every defect found recently sat in code the suite *executed*. The
OR-joined project clause ran in covered lines — the multi-project branch
was simply never taken. `due date:` was a covered line containing a
string the API rejects. Line coverage cannot see either. So a percentage
target, pursued directly, buys tests that assert what the code does
rather than what it should do, which is precisely how those bugs
survived.

Ranking the write tools by module coverage instead gives a different
list. The two most dangerous tools in the server — `bulk_update_execute`,
which rewrites a field across up to 100 issues, and `bulk_rollback`, the
only way back — sat at **10%**.

## Decision

Cover by blast radius, starting with the destructive write paths, and
read the uncovered lines rather than merely executing them.

Reading `bulk.py` found three defects in the undo path, none of which a
coverage tool would have reported:

1. **The rollback window could not contain its own batch.** Reverts were
   limited to `batch_start + 60s`, while execute issues two sequential
   commands per issue — roughly 200 round trips for a full batch. Every
   change landing after the first minute was left in place.
2. **The batch tag was removed anyway.** `untag` ran unconditionally, so
   an issue that kept its change lost the only marker identifying it as
   part of the batch. The rollback could not even be retried.
3. **Reverting nothing reported as completion.** "Changes reverted: 0"
   with no warning — a silent zero in the one tool where being wrong is
   least recoverable.

Fixed: the window is 30 minutes and anchored per issue to when *that*
issue was tagged; `untag` happens only for issues whose changes were
actually reverted; reverting nothing says so in terms that cannot be read
as success.

## Consequences

- `bulk.py` 10% → 76%; overall 56% → 57%. That single point is the honest
  shape of this work: the global figure is dominated by large read-only
  rendering modules, while the risk is concentrated in small write ones.
- 963 tests. Both rollback fixes are mutation-verified — restoring the
  60-second window or the unconditional untag turns a test red.
- The tests pin semantics rather than strings: that preview performs no
  writes and shares execute's cap, that an issue which could not be
  tagged is never mutated, that a per-issue command failure does not
  silence its neighbours.
- Remaining order, by the same weighting rather than by percentage:
  sprints (7%), articles (13%), projects (26%), history (30%). The
  formatting-heavy reporting modules come last, and their percentage is
  worth less than it looks.
