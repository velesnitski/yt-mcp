"""Config loading, paths, approver lookup, audit log, quarter math."""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


_CONFIG_DIR = Path(os.environ.get("YT_MCP_CONFIG_DIR", str(Path.home() / ".yt-mcp")))
_MANAGERS_FILE = _CONFIG_DIR / "managers.json"
_MANAGERS_SUGGESTED_FILE = _CONFIG_DIR / "managers.suggested.json"
_POLICY_FILE = _CONFIG_DIR / "policy.json"
_AUDIT_LOG = _CONFIG_DIR / "deadline-audit.log"

_QUARTER_RE = re.compile(r"^(\d{4})Q([1-4])$")

# Metadata keys at the top level of the JSON config are stripped from the
# user-entry view. `__default__` is the only top-level non-user key that
# stays — it's a user-facing fallback approver.
_METADATA_KEY = "_metadata"


def _load_managers_config() -> tuple[dict[str, Any], dict[str, Any]]:
    """Load managers.json (preferred) or managers.suggested.json (fallback).

    Returns ``(user_entries, metadata)``. The user entries dict contains
    ``__default__`` plus one entry per assignee login. Metadata is a
    side-channel for diagnostic info: ``source_file``, plus anything written
    by ``suggest_managers`` under the ``_metadata`` key.
    """
    for path in (_MANAGERS_FILE, _MANAGERS_SUGGESTED_FILE):
        if not path.exists():
            continue
        try:
            with path.open() as f:
                raw = json.load(f)
        except (OSError, ValueError) as e:
            print(
                f"[yt-mcp] WARN: could not parse {path}: {e}",
                file=sys.stderr,
            )
            continue
        if not isinstance(raw, dict):
            continue
        metadata = dict(raw.pop(_METADATA_KEY, {}) or {})
        metadata["source_file"] = str(path)
        return raw, metadata
    return {}, {"source_file": ""}


def _load_policy() -> dict[str, Any]:
    if not _POLICY_FILE.exists():
        return {}
    try:
        with _POLICY_FILE.open() as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as e:
        print(f"[yt-mcp] WARN: could not parse {_POLICY_FILE}: {e}", file=sys.stderr)
        return {}


def _audit(operator: str, tool: str, scope: dict, result_size: int) -> None:
    try:
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(tz=timezone.utc).isoformat(),
            "operator": operator,
            "tool": tool,
            "scope": scope,
            "result_size": result_size,
        }
        with _AUDIT_LOG.open("a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _get_approvers(login: str, config: dict) -> tuple[set[str], bool]:
    """Return (set of valid approver logins, manual_review flag) for an assignee."""
    entry = config.get(login)
    if entry is None or not isinstance(entry, dict):
        default = config.get("__default__")
        return ({default} if isinstance(default, str) else set()), False
    approvers: set[str] = set()
    primary = entry.get("primary")
    if isinstance(primary, str) and primary:
        approvers.add(primary)
    for acc in entry.get("also_accept") or []:
        if isinstance(acc, str) and acc:
            approvers.add(acc)
    return approvers, bool(entry.get("manual_review"))


def _get_reports(manager_login: str, config: dict) -> list[str]:
    """Reverse lookup: assignees whose primary is the given manager."""
    out: list[str] = []
    for login, entry in config.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("primary") == manager_login:
            out.append(login)
    return sorted(out)


def _quarter_to_range(quarter: str) -> tuple[datetime, datetime]:
    m = _QUARTER_RE.match(quarter)
    if not m:
        raise ValueError(f"Invalid quarter '{quarter}'. Expected '2026Q2'.")
    year, q = int(m.group(1)), int(m.group(2))
    start_month = (q - 1) * 3 + 1
    end_month = q * 3
    start = datetime(year, start_month, 1, tzinfo=timezone.utc)
    next_month_year = year + (1 if end_month == 12 else 0)
    next_month = 1 if end_month == 12 else end_month + 1
    end = datetime(next_month_year, next_month, 1, tzinfo=timezone.utc) - timedelta(seconds=1)
    return start, end


def _current_quarter() -> str:
    now = datetime.now(tz=timezone.utc)
    return f"{now.year}Q{(now.month - 1) // 3 + 1}"


def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _policy_effective_ms(policy: dict) -> int:
    raw = policy.get("policy_effective_date")
    if not raw:
        return 0
    dt = _parse_iso(raw)
    return int(dt.timestamp() * 1000) if dt else 0
