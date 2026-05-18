# 004 — Demote YouTrack 4xx responses from ERROR to WARNING

## Context

Production Sentry was receiving recurring events of the form:

```
YouTrack query error (400): Can't parse search query, please check and update query syntax
```

triggered by callers (LLMs) attempting search queries with invalid YouTrack
syntax — e.g. `project: PROJ created by: Some_User keyword` where the
underscored login made YT fail to parse the `created by:` clause.

The Sentry scrub function (`_scrub_event` in `logging.py`) already lists
`"YouTrack query error"` in `_USER_INPUT_VALUE_ERROR_PATTERNS`, so the events
*should* have been dropped. They were not, because the guard only fires when
`hint["exc_info"]` is populated.

In `client.py:_handle_error`, we do:

```python
_logger.error(str(error), extra={...})
raise error
```

The `_logger.error` call fires before the `raise`. Sentry's
`LoggingIntegration` captures the log record at that point and `exc_info` is
`None` — no exception is in flight yet. The scrub guard sees no exception,
lets the event through, and the noise reaches Sentry.

## Decision

Two complementary changes:

1. **`client.py`**: change `_logger.error` → `_logger.warning` for 400/404
   responses. These are caller errors (bad query syntax, missing issue) — not
   bugs in yt-mcp. Sentry's `LoggingIntegration` only escalates `>=ERROR` to
   events by default; warnings remain as breadcrumbs (still visible in the
   context of any *real* error, but no longer trigger their own event).
   The `ValueError` is still raised so the tool caller still sees the failure.

2. **`logging.py`**: extend `_scrub_event` with a fallback branch that checks
   `event["logentry"]["formatted"]` / `["message"]` against the same
   user-input pattern list. This catches any future regression where a
   logger call goes back to `ERROR` level without `exc_info`, so the event
   gets dropped at Sentry's `before_send` regardless.

## Alternatives considered

- **Add `exc_info=True` to the `_logger.error` call.** Would let the
  existing `_is_user_input_error` branch see the exception. But the
  exception isn't actually in flight yet (no `try/except` wraps it), so
  passing `exc_info=True` is misleading and Python would log a fake
  traceback rooted at the logger call. Rejected.

- **Suppress all 4xx silently.** Loses ability to see them in
  `~/.yt-mcp/yt-mcp.log` and in Sentry breadcrumbs of unrelated errors.
  Warnings keep both signals.

- **Tighten YouTrack query validation client-side.** Would catch some
  cases but YT syntax is extensive (commas, braces, parentheses, multi-
  token fields, user-resolvers); validating it correctly would mean
  reimplementing YT's parser. Server-side authority is the right
  validator — we just shouldn't surface its 400s as bugs.

## Consequences

- Tests: 319 → 328 (+9). New `tests/test_client_logging.py` covers both
  the client-side log level and the logentry fallback in `_scrub_event`.
- Sentry stops receiving recurring "Can't parse search query" noise.
- Real failures (500s, network errors, code bugs) still surface as
  events; only 400/404 are quieted.
- The error message returned to the MCP caller is unchanged — they still
  see the YouTrack-provided diagnostic and can correct the query.
- The local rotating log at `~/.yt-mcp/yt-mcp.log` still records these
  as WARNING entries, so on-disk forensics are preserved.
