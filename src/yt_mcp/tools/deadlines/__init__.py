"""Deadline-control tools — see docs/adr/002-deadline-control.md.

Package layout:
    config.py     — paths, load_managers_config, audit log, quarter helpers
    parser.py     — pure: classify_shift, extractors, regex constants
    fetcher.py    — async client wrappers + field selectors
    render.py     — markdown renderers
    audit.py      — audit_deadline_changes tool
    scorecard.py  — deadline_scorecard tool
    suggester.py  — suggest_managers tool
"""

from yt_mcp.resolver import InstanceResolver
from yt_mcp.tools.deadlines import audit as _audit_mod
from yt_mcp.tools.deadlines import scorecard as _scorecard_mod
from yt_mcp.tools.deadlines import suggester as _suggester_mod

# Backward-compat re-exports for the test surface and any external callers.
from yt_mcp.tools.deadlines.config import (  # noqa: F401
    _AUDIT_LOG,
    _CONFIG_DIR,
    _MANAGERS_FILE,
    _MANAGERS_SUGGESTED_FILE,
    _POLICY_FILE,
    _QUARTER_RE,
    _audit,
    _current_quarter,
    _get_approvers,
    _get_reports,
    _load_managers_config,
    _load_policy,
    _parse_iso,
    _quarter_to_range,
)
from yt_mcp.tools.deadlines.parser import (  # noqa: F401
    _classify_shift,
    _compile_standup_patterns,
    _extract_activity_date,
    _extract_deadline_ts,
    _format_date,
    _is_deadline_field,
    _is_standup,
)


def register(mcp, resolver: InstanceResolver) -> None:
    _audit_mod.register(mcp, resolver)
    _scorecard_mod.register(mcp, resolver)
    _suggester_mod.register(mcp, resolver)
