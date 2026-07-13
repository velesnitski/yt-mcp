# 027 — Confine caller-controlled filesystem paths (CWE-22 / CWE-73)

## Context

An external researcher (Moshe Levi, Levinity Cyber) reported, under
responsible disclosure, a path-traversal / external-control-of-file-path
issue in the `add_attachment` MCP tool. Validated from source and confirmed:

- `add_attachment`'s `file_path` mode did only `os.path.isfile()` then
  `open(file_path, "rb")` with **no confinement**, and the bytes flow to
  `POST /api/issues/{id}/attachments`. So a caller who controls the tool
  arguments — a compromised MCP client, or an LLM steered by **indirect
  prompt injection** — could read any file the process can access
  (`/etc/passwd`, `~/.ssh/id_rsa`, `.env`, cloud creds) and **exfiltrate** it
  into a YouTrack issue they can read. This is the classic prompt-injection
  "lethal trifecta" collapsed into one tool: private-data read **and** an
  external egress channel.
- A full-sweep of sibling sinks found a second, lower-severity instance:
  `monitoring.py` interpolated the caller-supplied `project` into
  `_SNAPSHOTS_DIR / f"{project.lower()}.json"` with no separator check, so
  `project="../../../x"` traversed out of the snapshots dir (an arbitrary
  `.json` *write* via `get_project_health`). The `deadlines/config.py` sinks
  use fixed operator-set paths and are not caller-controlled.

**Severity (honest calibration).** CWE-22/CWE-73. The reporter's CVSS 7.5
(`AV:N`) applies to the SSE/streamable-HTTP multi-tenant transport; for the
default **stdio-local** deployment `AV:L` → ~6.2 is more accurate (the
process already runs as the user, so the real risk is the confused-deputy /
prompt-injection exfil, not a remote privilege crossing). Either way it is a
legitimate hardening for an agentic tool and warrants a fix + advisory.

## Decision

**Secure by default; opt-in for the powerful mode.**

1. `add_attachment` `file_path` mode is **disabled unless the operator
   allowlists directories** via `YOUTRACK_ATTACHMENT_ROOTS` (os.pathsep
   list). When set, the path is `realpath`-resolved (which resolves symlinks
   *before* the check) and must be contained in a root by `os.path.commonpath`
   (not `str.startswith`, so `/root` vs `/root-evil` can't be confused).
   Absolute escapes, `../` traversal, and symlink-out are all refused. A
   100 MB read cap guards against multi-GB reads. The `content` /
   `content_base64` mode — where the file is read in the **client's** trust
   context, not silently by this server — is unchanged and is the
   recommended path; the docstring now says so.
2. `monitoring.py` snapshot filenames go through `_snapshot_path()`, which
   rejects any `project` that isn't a plain short-name token
   (`^[A-Za-z0-9_-]+$`). Unsafe names no-op snapshot tracking rather than
   read/write an attacker-chosen path — no behavior change for real projects
   (plain alphanumeric short-names).

### Why not just base-dir-confine to cwd (the report's option 1)?

The server's cwd can be `$HOME` or `/` depending on how the MCP client
launches it — confining to cwd would then still expose `~/.ssh` or `/etc`.
Default-deny with an explicit operator allowlist has no such footgun, and the
common agentic use (uploading generated content) already uses `content` mode.

## Consequences

- The reported arbitrary-file-read + exfil path is closed by default; a fresh
  install cannot read `/etc/passwd` via `file_path` at all. Operators who
  genuinely upload workspace files set `YOUTRACK_ATTACHMENT_ROOTS=/their/dir`.
- **Breaking change** for existing `file_path` users (must set the env var or
  switch to `content`) — justified as a security fix; the error message is
  actionable.
- Snapshot traversal closed with zero impact on valid projects.
- Tests 769 → 779: confinement (default-disabled, absolute escape, `../`
  traversal, symlink-out, sibling-prefix, in-root allowed, content-mode
  unaffected, not-found-within-root) + snapshot-name confinement. No
  sensitive real file is touched by any test — all run against tmp roots. The
  reporter's PoC was **not** executed.
- Patch/minor bump per release. Coordinated disclosure: advisory + CVE to be
  filed via GitHub Security Advisory, crediting the reporter.
