# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.2] - 2026-03-19

### Changed
- Split `dashboard.py` (711 lines) into `dashboard.py` (scoring tools) + `monitoring.py` (digest + at-risk tools)
- Extract shared helpers (`compile_exclude_patterns`, `should_exclude`, `ISSUE_FIELDS`, `ACTIVE_STATES`) to `formatters.py`
- Replace manual score+sort loops with `sorted()` generator expressions
- Move `_fmt_line` closure out of loop to module-level `_format_at_risk_line` function
- Use `frozenset` for state-set lookups in monitoring

## [1.2.1] - 2026-03-19

### Changed
- `get_at_risk_issues` — separate "Stalled" (In Progress/In Review/Ready for Test, high urgency) from "Forgotten" (Submitted/Pause/To Do idle 30d+, lower urgency)
- Add `limit_per_category` param (default: 10) and `forgotten_days` param (default: 30)
- Show "...and N more" when categories are truncated

## [1.2.0] - 2026-03-19

### Added
- `get_issues_digest` tool — shows recent changes (state, comments, fields, links) for any set of issues since a given time
- `get_at_risk_issues` tool — finds stalled issues (no activity in N days), overdue deadlines, approaching deadlines, and over-estimate issues
- Supports duration (`24h`, `7d`, `30m`) and date (`2026-03-18`) for the `since` parameter
- Fetches activity for all matching issues in parallel with `asyncio.gather`
- Multi-product scoring factor: +10 per additional product (cap +30)
- Blocking-others scoring factor: +20 per inward Depend link (cap +80)

### Fixed
- `get_top_active_issues` query compatibility — now uses `#Unresolved` with client-side state filtering

## [1.1.2] - 2026-03-19

### Fixed
- Fix `get_top_active_issues` query failure on some YouTrack instances — now fetches all unresolved issues and filters by state client-side

### Added
- "Pause" state support in scoring model and dashboard (treated as active, state bonus = 0)

## [1.1.1] - 2026-03-18

### Fixed
- Fix `execute_command` 404 error on some YouTrack versions — switch from `/api/issues/{id}/execute` to standard `/api/commands` endpoint

### Changed
- Enable HTTP/2 for YouTrack API connections (`httpx[http2]`) — multiplexed requests over single TCP connection
- Configure connection pooling (20 max connections, 10 keepalive, 30s expiry)
- Parallelize activities + issue info fetches in `get_issue_changes_summary` with `asyncio.gather`
- Content-Type header moved to module-level constant

## [1.1.0] - 2026-03-18

### Added
- **Scoring module** (`scoring.py`) with configurable weighted models for active and blocked issues
- `get_top_active_issues` — rank active issues by priority, type, state, tags, staleness, and blocker count
- `get_top_blocked_issues` — rank blocked issues by priority, type, tags, blocked duration, and blocker count
- `get_team_dashboard` — combined project brief with top active, top blocked, and summary stats
- Noise filtering via `exclude_patterns` parameter (regex, e.g., exclude daily reports)
- 31 unit tests for scoring logic (helpers, active model, blocked model, edge cases)

## [1.0.2] - 2026-03-17

### Added
- `poll_changes` tool for automation triggers (Make.com, n8n, cron) — returns issues updated within the last N minutes
- Make.com setup instructions in README

## [1.0.1] - 2026-03-17

### Changed
- Precompile regex patterns at module level in `projects.py` and `translate.py` instead of recompiling per function call
- Move `import re` and `from datetime` to module level in `projects.py` (was inside async functions)
- Remove redundant inline import in `issues.py` (`_resolve_state`, `_resolve_assignee` already imported at top)
- Parallelize independent API calls in `impact.py` using `asyncio.gather` (mentions + same-product searches)
- Cache timestamp lookup in `bulk.py` list comprehension (walrus operator) to avoid double `.get()`

## [1.0.0] - 2026-03-17

### Added
- **Multi-instance support** — connect multiple YouTrack instances with `YOUTRACK_INSTANCES` env var, auto-detection from URLs, and optional `instance` parameter on all tools
- **43 tools** across 8 categories: Issues (16), Time tracking (4), Agile boards (5), Projects & users (3), Knowledge Base (8), Bulk operations (3), Translation (2), Impact analysis (2)
- **Issue URL support** — paste full YouTrack URLs instead of issue IDs in any tool
- **Board URL support** — paste agile board URLs for `get_agile_board` and `get_sprint_board`
- **Generic field updates** — `update_issue` accepts any YouTrack command string via `command` parameter
- **Rollback support** — all write operations return previous values for undo
- **Bulk operations** — batch tag system (`yt-mcp-{timestamp}`) with `bulk_rollback`
- **Translation workflow** — `get_issues_for_translation` + `apply_translations` with batch rollback
- **Knowledge Base CRUD** — full article and article comment management
- **User tools** — `get_current_user` and `search_users`
- **Impact analysis** — cross-product dependency graphs and deadline impact analysis
- **Issue templates** — 9 built-in templates (bug, feature, task, daily, spike, release, devops, incident, epic)
- **Read-only mode** — `YOUTRACK_READ_ONLY=true` blocks all write operations
- **Tool filtering** — `DISABLED_TOOLS` to remove specific tools
- **Security** — HTTPS enforcement, error message truncation, batch tag validation
- **Transport options** — stdio (default), SSE, and streamable-http
- **106 tests** with GitHub Actions CI (Python 3.10–3.13)

[1.2.2]: https://github.com/velesnitski/yt-mcp/compare/v1.2.1...v1.2.2
[1.2.1]: https://github.com/velesnitski/yt-mcp/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/velesnitski/yt-mcp/compare/v1.1.2...v1.2.0
[1.1.2]: https://github.com/velesnitski/yt-mcp/compare/v1.1.1...v1.1.2
[1.1.1]: https://github.com/velesnitski/yt-mcp/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/velesnitski/yt-mcp/compare/v1.0.2...v1.1.0
[1.0.2]: https://github.com/velesnitski/yt-mcp/compare/v1.0.1...v1.0.2
[1.0.1]: https://github.com/velesnitski/yt-mcp/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/velesnitski/yt-mcp/releases/tag/v1.0.0
