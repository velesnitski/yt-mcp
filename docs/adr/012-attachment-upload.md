# 012 — Attachment upload (`add_attachment`)

## Context

The attachment surface was read-only: `list_attachments` and
`get_attachment_url`. When an agent wanted to "attach a report" to an
issue, there was no upload path — it had to fall back to a markdown
`add_comment`.

For a **text/markdown** report that fallback is actually the *better*
medium (renders inline, full-text searchable, notifies watchers). But for
a **binary** artifact — a generated HTML report with charts, an Excel
export, a PDF, a screenshot — a comment can't carry it, and there was no
way to get the file onto the issue at all.

The blocker was structural: `YouTrackClient` is JSON-only
(`_JSON_HEADERS = application/json`, `post()` always sends `json=`).
YouTrack's attachment upload is `POST /api/issues/{id}/attachments` as
**multipart/form-data**, which that client couldn't emit.

## Decision

### Client: a multipart POST

New `YouTrackClient.post_multipart(path, files, params)` that passes
httpx's `files=` and deliberately does **not** set the JSON content-type —
httpx then derives the multipart boundary itself. Routes through the same
`_handle_error` as every other call (so a 400/404 surfaces the YT message
and is scrubbed from Sentry like other user-input errors).

### Tool: `add_attachment` (write)

Dual input mode — covers "I have a file" and "I generated content in
memory" without forcing a temp file:

- **`file_path`** — upload an existing file from disk. Name defaults to
  the basename; `filename` overrides. MIME from the extension.
- **`content` + `filename`** — upload inline content. UTF-8 text by
  default; `content_base64=True` decodes binary the caller already holds
  as base64 (e.g. a screenshot). MIME from the extension, override via
  `mime_type`.

Guards (all fail before any API call): exactly one of file_path/content;
filename required for content; file-exists check; base64 validation;
empty-payload refusal. The relative attachment URL in the response is
expanded to an absolute link via `client.base_url` (the property added in
v1.12.0), so the caller gets a clickable URL back.

The docstring steers callers toward `add_comment` for plain text and
reserves this for binary/downloadable artifacts — so the tool's existence
doesn't make "post a comment" the wrong instinct for text reports.

Registered in `WRITE_TOOLS` — blocked in read-only mode, and the
confirm-before-write rule applies.

## Alternatives considered

- **Attach to a comment instead of the issue.** YouTrack's comment-
  attachment flow is a two-step upload-then-reference dance; the issue
  attachments endpoint is one call and is what "attach to the ticket"
  means in practice. Skipped the comment variant as YAGNI.
- **Parse `presentation`/temp-file-only.** Requiring a temp file on disk
  for generated content is friction; the inline `content` mode removes it.
- **Build it earlier, speculatively.** Deliberately deferred until a
  concrete need (a report-to-ticket flow) — the read-only surface was
  fine until binary attach was actually wanted.

## Consequences

- Tool count: 77 → 78. New write tool; client gains one method.
- Test count: 680 → 699 (+19 in `tests/test_attachments.py`): MIME guess,
  URL expansion, full validation matrix (no-input, both-inputs, missing
  filename, file-not-found, bad base64), text-utf8, base64-binary, MIME
  override, disk-file read, filename override, list-or-dict response shape,
  issue-URL parsing.
- Minor bump 1.15.1 → 1.16.0 (new tool).
- Tidy: the existing `get_attachment_url` now uses the `client.base_url`
  property too, replacing a `client._config.url` poke (shared `_full_url`
  helper).
