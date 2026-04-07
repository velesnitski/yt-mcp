# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.7.0] - 2026-04-07

### Added
- `get_project_fields` — list custom fields with required status and valid values (use before `create_issue` to discover correct field names)
- **Draft-based issue creation** — projects with required custom fields (Subsystem, Type, Product, etc.) now work via automatic draft → command → publish flow
- **3-level command strategy** — full command as-is → split into individual fields → rejoin failed splits (handles emoji field names like `Evaluation time 🕙`)
- **Graceful command failures** — unsettable fields reported with "Could not set" guidance instead of aborting
- **Required fields info on publish failure** — fetches and displays all required fields with valid values
- **GitHub Copilot** setup instructions (`.vscode/mcp.json`)
- **Cursor** setup instructions (`.cursor/mcp.json`)
- **JetBrains IDEs** setup instructions (Settings → AI Assistant → MCP)

### Fixed
- `create_issue` 400 error on projects with required custom fields (Subsystem, Type, Phase, etc.)
- YouTrack command API brace handling — braces treated as literal chars, now sent without braces
- Multi-word product values no longer break command parsing
- Draft publish returns correct `idReadable` (was returning "?")

### Changed
- **54 tools** (was 53) — added `get_project_fields`
- Removed dead code from abandoned customFields $type approach (~270 lines)
- Replaced real project prefixes with generic ones in tests and docs

## [1.6.0] - 2026-03-27

### Added
- `get_project_health` — project health report with state/product distribution, health metrics (%), and recently resolved issues
- **Unestimated** and **Ancient** (>200d) categories in `get_at_risk_issues`
- **Compact mode** — set `YOUTRACK_COMPACT=1` to strip markdown from responses (~60% token savings)
- **Tool call analytics** — every call logged to `~/.yt-mcp/analytics.log` with response size and error details
- **Sentry breadcrumbs** — tool calls visible in error context
- Trimmed all tool docstrings (~22% reduction in tool definition tokens)

### Changed
- Split `dashboard.py` into `dashboard.py` (scoring) + `monitoring.py` (digest + at-risk)
- Shared helpers (`compile_exclude_patterns`, `should_exclude`, `ISSUE_FIELDS`) moved to `formatters.py`
- `get_at_risk_issues` — separate "Stalled" (In Progress, high urgency) from "Forgotten" (Submitted/Pause, lower urgency), add `limit_per_category` and `forgotten_days` params

## [1.5.0] - 2026-03-20

### Added
- `group_by_product` parameter on scoring tools — group results by Product field
- `get_multi_team_dashboard` — combined dashboard for multiple projects in one call (parallel fetch)
- **Structured JSON logging** to stderr and `~/.yt-mcp/yt-mcp.log` (always on)
- **Sentry error tracking** — set `SENTRY_DSN` env var (SDK included as dependency)
- **Persistent instance ID** (`~/.yt-mcp/instance_id`) for distinguishing machines

## [1.4.0] - 2026-03-19

### Added
- **OAuth 2.0 for claude.ai connectors** — set `YOUTRACK_OAUTH_URL` to enable
- **Access code gate** — set `YOUTRACK_ACCESS_CODE` for password-protected OAuth
- `check_task_creation` — verify a task was created with quality score (0–10)
- `get_creation_activity` — recently created issues with quality stats
- `Dockerfile`, `docker-compose.yml` for SSE/HTTP deployments
- CSRF protection, timing-safe comparisons, session expiry on OAuth form

## [1.3.0] - 2026-03-19

### Added
- `get_issues_digest` — recent changes for any set of issues (state, comments, fields, links)
- `get_at_risk_issues` — stalled, overdue, approaching deadline, over estimate detection
- Multi-product scoring: +10 per additional product (cap +30)
- Blocking-others scoring: +20 per inward Depend link (cap +80)
- `poll_changes` — poll for recently changed issues (automation triggers)
- Make.com setup instructions

### Fixed
- `get_top_active_issues` query — uses `#Unresolved` with client-side state filtering
- `poll_changes` query — client-side timestamp filtering

## [1.2.0] - 2026-03-18

### Added
- **Scoring module** with weighted models for active and blocked issues
- `get_top_active_issues`, `get_top_blocked_issues`, `get_team_dashboard`
- Noise filtering via `exclude_patterns` (regex)
- "Pause" state support (treated as active, state bonus = 0)
- Community files: CONTRIBUTING.md, CODE_OF_CONDUCT.md, SECURITY.md, issue/PR templates

### Fixed
- `execute_command` — switch to `/api/commands` endpoint (works across all YouTrack versions)

### Changed
- Enable HTTP/2 (`httpx[http2]`) with connection pooling
- Precompile regex at module level, parallelize independent API calls with `asyncio.gather`

## [1.0.0] - 2026-03-17

### Added
- **53 tools** across 9 categories: Issues (18), Time tracking (4), Agile boards (5), Projects & users (3), Knowledge Base (8), Bulk operations (3), Translation (2), Priority dashboard & monitoring (9), Impact analysis (2)
- **Multi-instance support** — `YOUTRACK_INSTANCES` env var with URL auto-detection
- **Issue/Board URL support** — paste YouTrack URLs instead of IDs
- **Generic field updates** via YouTrack command syntax
- **Rollback support** — all write operations return previous values
- **Bulk operations** with batch tag rollback
- **Translation workflow** with batch rollback
- **Read-only mode** and per-tool filtering
- **HTTPS enforcement** and error message truncation
- **168 tests** with GitHub Actions CI (Python 3.10–3.13)

[1.7.0]: https://github.com/velesnitski/yt-mcp/compare/v1.6.0...v1.7.0
[1.6.0]: https://github.com/velesnitski/yt-mcp/compare/v1.5.0...v1.6.0
[1.5.0]: https://github.com/velesnitski/yt-mcp/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/velesnitski/yt-mcp/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/velesnitski/yt-mcp/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/velesnitski/yt-mcp/compare/v1.0.0...v1.2.0
[1.0.0]: https://github.com/velesnitski/yt-mcp/releases/tag/v1.0.0
