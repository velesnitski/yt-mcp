# ADR-001: Draft-based issue creation for required custom fields

## Status
Accepted

## Context
YouTrack projects can have required custom fields (Type, Subsystem, Product, Phase, Evaluation time, etc.). The `POST /api/issues` endpoint validates all required fields at creation time and rejects the request with 400 if any are missing. The YouTrack command API (`POST /api/commands`) can set these fields but requires an existing issue to operate on — creating a chicken-and-egg problem.

### Approaches considered

1. **Set fields via REST body with `$type`** — requires correct `$type` discriminator for each field, which varies across YouTrack versions and instances. Abandoned after "Incompatible field type" and "Error in field unknown" errors.

2. **Random `draftId` parameter** — `POST /api/issues?draftId=<uuid>` returns 404 because YouTrack expects a real draft.

3. **Draft → command → publish** (chosen) — create a draft via `/api/users/me/drafts` (no validation), apply commands to set required fields, then publish via `/api/issues?draftId=<id>`.

## Decision
Use the draft-based approach with a 3-level command strategy:

1. **Full command** — send the user's original command as-is (handles multi-word and emoji field names like `Evaluation time 🕙 1h`)
2. **Split** — regex-split into individual field-value pairs (handles parser ambiguity like `Subsystem Frontend Type Task`)
3. **Rejoin failed splits** — recombine failed split parts and retry (recovers incorrectly split multi-word fields)

Product is always a separate command call (multi-word values without braces).

Failed commands are collected and reported in the response instead of aborting creation.

## Consequences
- Projects with required fields now work without manual intervention
- One extra API call (draft creation) on the fallback path
- Emoji field names work when the LLM includes the emoji in the command
- Fields that can't be set via command API at all are reported with valid values from `get_project_fields`
