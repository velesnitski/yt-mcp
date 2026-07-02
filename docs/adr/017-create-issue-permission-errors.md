# 017 — create_issue: handle insufficient-permission (401/403) failures

## Context

Follow-up to ADR-016. Triple-checking `create_issue` for the reported
"can't set a required field (Department)" complaint surfaced a second, more
serious failure mode: **what happens when the caller's account lacks the
permission** to create the issue, or to set a restricted field.

`client._handle_error` only builds a friendly `ValueError` for **400/404**.
Every other status — including **401/403 (permission denied)** and 5xx —
falls through to `resp.raise_for_status()`, which raises
`httpx.HTTPStatusError`. But `create_issue` / `_apply_commands` caught **only
`ValueError`**. So a permission failure was never handled:

- **Account can create but can't set a field** (e.g. Department is
  permission-restricted): `POST /api/issues` succeeds → the issue is created
  → the field command returns 403 → **uncaught** → the tool raises a raw
  httpx error. The issue is left **orphaned and bare**, the friendly
  "Could not set" path never runs, and the split fallback is skipped.
  Retries pile up duplicate empty issues. This is exactly the "can't set
  Department" symptom when the end-user's account is permission-restricted —
  manifesting as a crash rather than a clear message.
- **Account can't create at all**: `POST /api/issues` returns 403 → raw
  httpx error that also **leaks the instance host URL** in its string.

The rest of the codebase already had the right convention: `bulk.py` and
`translate.py` catch `(httpx.HTTPStatusError, ValueError)` on the same
`/api/commands` endpoint. `issues.py`'s create path simply hadn't adopted it.

## Decision

1. Catch `(httpx.HTTPStatusError, ValueError)` at every command-application
   site in `_apply_commands` (product, whole-command, split, rejoin), matching
   `bulk.py` / `translate.py`. A field the account can't set now lands in
   `failed_commands` and is surfaced as **"Could not set: … HTTP 403
   (insufficient permissions)"** — the issue is still reported as created, no
   exception escapes, no orphan-by-crash.
2. Add a dedicated `except httpx.HTTPStatusError` on the create call itself:
   401/403 returns a clean, actionable **"insufficient permissions to create
   issues in project X (HTTP 403)"** message and — critically — does **not**
   fall into the draft path (which would leave an orphaned draft). Non-auth
   HTTP errors (5xx) re-raise unchanged.
3. New `_cmd_error_text()` helper renders a caught error as clean text:
   `ValueError` keeps its already-truncated YouTrack message;
   `HTTPStatusError` is reduced to `HTTP <code> (<reason>)` so the request
   **URL never leaks** into tool output (the client truncates 400/404 bodies
   for the same reason).
4. Broaden the draft-publish `except` to `(httpx.HTTPStatusError, ValueError)`
   and route it through `_cmd_error_text()` too.

## Alternatives considered

- **Broaden `client._handle_error` to turn 401/403 into `ValueError`.**
  Would fix create_issue for free, but changes global error semantics for
  every tool (many of which distinguish the two, or rely on
  `HTTPStatusError` for ret/telemetry). Too broad for this bug; the localized
  catch mirrors the existing bulk/translate convention.
- **Pre-flight permission check before creating.** An extra round-trip per
  create to read the caller's project permissions; racy and slower. Reacting
  to the actual 403 is simpler and authoritative.

## Consequences

- Permission-denied on a field → the issue is created and the field is
  reported under "Could not set: … HTTP 403 (insufficient permissions)",
  never a crash or an orphan-by-exception.
- Permission-denied on create → one clean actionable line, no raw error, no
  URL leak, and no orphaned draft.
- 5xx on create still propagates (not masked as a permission problem).
- Tests: 711 → 714 (+3 in `test_issues.py`): field-command 403 is reported
  not raised, create-403 returns a clean URL-free message, and create-500
  still raises. Driven through the real `create_issue` with a mock client
  that raises `httpx.HTTPStatusError`.
- Patch bump 1.16.6 → 1.16.7.
