# YouTrack API quirk registry

Shared between **yt-mcp** (`docs/YT_QUIRKS.md`) and **youtrack-reports**
(`docs/YT_QUIRKS.md`) — same file, same IDs. When either repo discovers a
new quirk, add it HERE in both copies in the same change. Every entry
names its guard (the test or code pattern that prevents reintroduction);
a quirk without a guard is a regression waiting to happen.

Why this exists: four separate incidents (Q1, Q2, Q3, Q4) were each
discovered by a production report silently breaking or silently lying.
The shared failure mode is **YouTrack Cloud rejecting or silently
ignoring things that used to work** — upgrades change parser/selector
behavior with no notice and no error.

| ID | Quirk | Symptom if ignored | Correct handling | Guard |
|----|-------|--------------------|------------------|-------|
| Q1 | `resolved:` alias broken for date ranges (since a 2026-07 Cloud upgrade) | Spaced range → 400; **unspaced range → parses but silently matches zero** (the trap variant) | Always query the canonical `resolved date:` attribute | Source-pinning tests in both repos (yt-mcp ADR-035; reports ADR 021 — word-boundary form, `Unresolved: {` in log lines false-positives the naive check) |
| Q2 | Clause order matters in composed queries: a `resolved date:` range must be the FIRST clause | Reversed order → parser rejection on composed queries | Put the date-range clause first; note single-clause forms tolerate both orders (verified live) | yt-mcp ADR-039 test; reports verified 2026-07-28, comment at query site |
| Q3 | Issue links carry **no top-level `state` field** — `state(name)` on linked issues is silently ignored | Every linked issue renders state `""` while tests mocking the imagined shape pass | Read State from `customFields`; fallback chain `top-level → custom field` | yt-mcp ADR-034 (`_linked_state`); reports `_resolve_native(...) or get_field(...)` |
| Q4 | Board-column nested `issues` selector silently ignored | Every sprint-board column renders `(0)` while the sprint holds issues | Fetch the sprint-level issue list; group into columns client-side | yt-mcp ADR-038 + behavior tests pinning the real API shape |
| Q5 | `tag:` clause 400s on instances where the referenced tags don't exist | Whole query fails on multi-instance deployments | Graceful retry without the tag clause; degrade, don't die | reports ADR 008 |
| Q6 | Null shapes: `linkType`, `author`, `added`, `summary` can be `null` where a dict/list/str is expected | `NoneType` crashes deep in parsing (or silent misclassification) | `(x.get("k") or {})` / `or ""` pattern at every read | Fixed fleet-wide after one crash class; null-shape unit tests in both repos |
| Q7 | `activities` `added` field is a **list for most categories, a bare object for some** | Comment texts silently missed (or crash) depending on category | Tolerate both shapes before reading | reports ADR 023 (`_added_texts`) + tests |
| Q8 | A `Board {Name}: *` query can return 0 for a board that visibly has cards | Board looks empty; downstream logic reports "nothing" | Query the board's underlying project(s) instead of the board clause | Observed 2026-07-29 (board probe); registry entry is the guard — no code path relies on board queries |
| Q9 | Custom field names may contain emoji and unicode (e.g. a deadline field with a skull emoji) | Name-matching by "clean" name silently finds nothing | Match the EXACT configured field name, emoji included; discover names from a live issue, never assume | Field-name constants copied from live payloads in both repos |
| Q10 | Workflow-bot display names are mutable; bot **login prefix** is structural; service comments written through the API are authored by the token's *human* account | Name-only filters silently start counting bot nags as human activity after a rename; service stamps read as real work | Filter by login prefix AND text stamp, not display name alone | yt-mcp ADR-037 (`split_service_comments`); reports ADR 023 (`_is_service_entry`) + tests |
| Q11 | `summary: <word>` search matches mentions, not just titled items | Keyword searches drown in mention-only noise | Shape-filter results with a summary regex after the query | yt-mcp ADR-039 (`_RELEASE_RE`); reports `RELEASE_RE` (kept in sync) |
| Q12 | Batch `#ID or #ID or …` queries work but degrade with size | Oversized batches fail or truncate silently | Chunk at ~40 IDs per query | reports `batch_issue_status` chunking |
| Q13 | Logins can be non-ASCII (Cyrillic logins observed in the wild) | ASCII assumptions in matching/sorting silently drop users | Treat logins as opaque unicode strings | Observed 2026-07-27 activity probe; no code path assumes ASCII |
| Q14 | `updated:` / `created:` spaced date ranges still work post-upgrade (unlike Q1) | Over-fixing Q1 by "migrating" these wastes the canonical forms | Leave them as-is; only `resolved:` was broken | yt-mcp ADR-035 scope note |
| Q15 | Selector-honored ≠ error-free: a wrong/unsupported nested field selector often returns **success with the field absent** | "No 400" verification proves nothing; features silently become dead code | Verify selectors by asserting the DATA comes back, not by absence of errors | reports ADR audit 2026-07-28 (`added(text)` proof); the Q3/Q4 incidents are this class |
| Q16 | POST create responses default to `$type,id` only — `idReadable` is absent unless requested | create reported "Created: **?**" and follow-up commands targeted idReadable "?" (the draft-publish response was fixed for exactly this; the direct path was missed) | Append `?fields=idReadable,summary` (or the fields you need) to every entity-creating POST | yt-mcp ADR-042: test asserts the request carries `fields=` + honest default-shape mock |

## Update protocol

1. Add the row here (both repos, same ID) in the same change as the fix.
2. Name the guard; if the guard is "registry entry only" (Q8, Q13), say so
   explicitly — that documents accepted risk, not an oversight.
3. When a Cloud upgrade breaks something new: bisect the query live before
   suspecting the code (a formerly-stable tool failing on ALL
   boards/instances at once is the upgrade signature — Q1's lesson).
