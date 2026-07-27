# 037 — find_comments tool + service-comment hygiene

## Context

Three gaps surfaced by live audit sessions:

1. **"The ticket where someone wrote …" had no tool.** Recovering a
   remembered comment took a `commenter:` query plus fetching and grepping
   comments per candidate issue by hand over raw REST. It's one of the most
   common manager-shaped questions against a tracker.
2. **Bot noise dominates comment reads.** On a typical long-running ticket,
   roughly a third of comments are workflow-automation nags (deadline /
   time-tracking reminders from `workflow_user…` accounts) and this
   server's own `[yt-mcp]` service stamps. `dedupe_comments` only collapses
   identical repeats — each nag has unique text, so they all pass through
   and burn context on every read.
3. **Compact mode truncates comments at a hardcoded 200 chars**, forcing a
   second fetch in JSON mode whenever the comments ARE the payload
   (meeting-prep flows).

## Decision

- **`find_comments(text, author, project, max_results)`** (tool #82, in the
  comments module): a YouTrack full-text query narrows candidates (plus
  `commenter:`/`project:` clauses when given), then comments are matched
  locally — whole phrase case-insensitively, all-words fallback — and
  returned newest-first as issue + author + date + snippet. Service
  comments never match. Empty `text` raises UserInputError (ADR-036).
- **`split_service_comments`** in formatters: hides comments whose author
  login starts with `workflow_user` or whose text starts with `[yt-mcp]`.
  `get_issue` applies it by default and always reports the hidden count
  (report note / `comments_hidden` in JSON) — nothing disappears silently.
  `include_bots=True` restores them.
- **`comment_chars` param** on `get_issue` (default 200, `0` = full),
  threaded into the compact renderer.

## Consequences

- 81 → 82 tools; comment reads shed the bot tail by default; the
  comment-archaeology workflow is one call.
- 830 tests pass (10 new). Minor release 1.20.0.
