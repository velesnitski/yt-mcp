# 018 — Low-permission project resolution: drop the dead non-admin fallbacks

## Context

Follow-up to ADR-017. The question raised: can a low-permission user (access
to one department/team/project) create an issue *without* the tool hitting an
admin-only API first? Answering it required checking what the project-lookup
endpoints actually require — and that overturned an assumption an earlier
draft of this ADR had made.

**Empirically verified against a live YouTrack instance (raw status codes):**

| Endpoint | Status |
| --- | --- |
| `GET /api/admin/projects` | **200** |
| `GET /api/projects` | **404 (route does not exist)** |
| `GET /api/admin/projects/{id}/customFields` | **200** |
| `GET /api/projects/{id}/customFields` | **404** |

**Confirmed against the JetBrains REST docs:** `GET /api/admin/projects` lists
"the Projects the user has access to." The GET carries **no permission
requirement** (only `POST` needs *Create Project*). The `admin` path segment
is a URL namespace, not a permission gate: a low-permission user gets their
own filtered subset (200), **not a 403**. (Custom-field detail may come back
empty for a non-admin, which only affects the optional required-fields hint.)

Two conclusions:

1. **A low-permission user can already resolve their project and create an
   issue.** `resolve_project_id` reads `/api/admin/projects`, which returns
   that user's one visible project. The real permission wall is at
   `POST /api/issues` (Create Issue) and `/api/commands` (field updates) —
   i.e. exactly what ADR-017 (v1.16.7) handles.
2. **The `/api/projects` fallbacks were dead code.** Both
   `resolve_project_id` and `_get_required_fields_info` looped
   `("/api/admin/projects…", "/api/projects…")`. The second member 404s on
   every instance — it never contributed a result. An earlier draft of this
   ADR mis-read the "admin" prefix as an admin gate and framed the fallback as
   the low-permission escape hatch. It is not: admin/projects does not 403 for
   low-permission users, and the fallback target does not exist.

## Decision

- **Remove the phantom `/api/projects` fallbacks** in both `resolve_project_id`
  and `_get_required_fields_info`; call `/api/admin/projects[/…]` only. This
  removes a guaranteed extra 404 round-trip whenever the primary path was
  retried, and stops implying a non-admin endpoint exists.
- **Keep a defensive catch** on the single call: `(httpx.HTTPStatusError,
  ValueError)`. If a genuinely access-less token *does* 403 (or the call
  otherwise fails), `resolve_project_id` degrades to `None` →
  `create_issue` returns a clean "Project not found" and never attempts the
  create, rather than propagating an uncaught error.
- `_get_required_fields_info` is explicitly best-effort: any failure
  (permission/parse/shape) yields `""` (the hint is optional).

## What did NOT need fixing

There is no admin-only pre-flight to avoid — `/api/admin/projects` *is* the
projects resource and it self-filters by the caller's permissions. So "don't
query the admin API first" resolves to "there is only one projects endpoint,
and it is safe for low-permission users." No reordering was possible or
needed.

## Consequences

- Low-permission create works off the caller's filtered project list; the
  nonexistent `/api/projects` is never called (asserted by test).
- One fewer wasted 404 on any retry path.
- Tests: 714 → 720. New faithful low-permission **integration** class drives
  the real `YouTrackClient` through an `httpx.MockTransport`: resolves the
  user's own project without touching `/api/projects`, reports a field-command
  403 cleanly, handles create-403, and degrades an access-less token (admin/
  projects 403) to "not found" without attempting a create. Plus the ADR-017
  gap tests (product-command 403, create-401).
- **Mutation testing:** 10 mutants (one per element of the ADR-017/018 fix),
  run with bytecode caching disabled (`python -B` + `PYTHONDONTWRITEBYTECODE=1`
  to avoid CPython's same-second stale-`.pyc` trap, which had produced a false
  "killed"). Result: **10/10 killed, 0 survived.**
- Patch bump 1.16.7 → 1.16.8.
