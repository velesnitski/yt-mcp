# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[1.0.2]: https://github.com/velesnitski/yt-mcp/compare/v1.0.1...v1.0.2
[1.0.1]: https://github.com/velesnitski/yt-mcp/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/velesnitski/yt-mcp/releases/tag/v1.0.0
