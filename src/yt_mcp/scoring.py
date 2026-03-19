"""Issue scoring models for priority dashboards.

Weights are tuned as module-level constants for easy adjustment.
"""

from datetime import datetime, timezone

from yt_mcp.formatters import _resolve_state, _get_custom_field

# --- Priority scores ---
PRIORITY_SCORES: dict[str, int] = {
    "show-stopper": 100,
    "critical": 80,
    "high": 60,
    "medium": 30,
    "normal": 15,
    "low": 5,
}

# --- Type bonuses ---
TYPE_BONUSES: dict[str, int] = {
    "bug": 20,
    "feature": 10,
    "task": 5,
}

# --- State bonuses (active model only) ---
STATE_BONUSES: dict[str, int] = {
    "in progress": 10,
    "in review": 5,
    "ready for test": 5,
    "submitted": 0,
    "pause": 0,
}

# --- Tag bonuses ---
TAG_BONUSES: dict[str, int] = {
    "critical": 40,
    "urgent": 30,
}

# --- Blocker scoring ---
BLOCKER_BONUS = 25
BLOCKER_CAP = 100

# --- Staleness thresholds (active model) ---
STALENESS_THRESHOLDS: list[tuple[int, int]] = [
    (14, 15),  # >14 days = +15
    (7, 10),   # >7 days = +10
    (3, 5),    # >3 days = +5
]

# --- Duration thresholds (blocked model) ---
BLOCKED_DURATION_THRESHOLDS: list[tuple[int, int, str]] = [
    (90, 30, "Frozen"),   # >90 days = +30
    (30, 20, "Long"),     # >30 days = +20
    (14, 15, "Stale"),    # >14 days = +15
    (7, 10, "Aging"),     # >7 days = +10
]


def _get_priority_name(issue: dict) -> str:
    """Extract priority name from issue data."""
    p = issue.get("priority")
    if p and isinstance(p, dict) and p.get("name"):
        return p["name"]
    return _get_custom_field(issue, "Priority") or ""


def _get_type_name(issue: dict) -> str:
    """Extract type name from issue data."""
    return _get_custom_field(issue, "Type") or ""


def _count_blockers(issue: dict) -> int:
    """Count issues blocked by this one (outward Subtask/Depend links)."""
    count = 0
    for link in issue.get("links", []):
        direction = link.get("direction", "")
        link_type = link.get("linkType", {}).get("name", "").lower()
        if direction == "OUTWARD" and any(
            kw in link_type for kw in ("subtask", "depend", "parent")
        ):
            count += len(link.get("issues", []))
    return count


def _days_since_update(issue: dict) -> int:
    """Calculate days since last update."""
    updated_ms = issue.get("updated")
    if not updated_ms:
        return 0
    updated_dt = datetime.fromtimestamp(updated_ms / 1000, tz=timezone.utc)
    return (datetime.now(tz=timezone.utc) - updated_dt).days


def compute_active_score(issue: dict) -> tuple[int, dict[str, int]]:
    """Score an active issue. Returns (total_score, breakdown).

    Breakdown keys: priority, type, state, tags, staleness, blockers.
    """
    breakdown: dict[str, int] = {}

    # Priority
    priority = _get_priority_name(issue).lower()
    breakdown["priority"] = PRIORITY_SCORES.get(priority, 0)

    # Type
    issue_type = _get_type_name(issue).lower()
    breakdown["type"] = TYPE_BONUSES.get(issue_type, 0)

    # State
    state = _resolve_state(issue).lower()
    breakdown["state"] = STATE_BONUSES.get(state, 0)

    # Tags
    tag_bonus = 0
    for tag in issue.get("tags", []):
        tag_name = tag.get("name", "").lower()
        tag_bonus += TAG_BONUSES.get(tag_name, 0)
    breakdown["tags"] = tag_bonus

    # Staleness
    days = _days_since_update(issue)
    staleness = 0
    for threshold_days, bonus in STALENESS_THRESHOLDS:
        if days > threshold_days:
            staleness = bonus
            break
    breakdown["staleness"] = staleness

    # Blockers
    blocker_count = _count_blockers(issue)
    breakdown["blockers"] = min(blocker_count * BLOCKER_BONUS, BLOCKER_CAP)

    total = sum(breakdown.values())
    return total, breakdown


def compute_blocked_score(issue: dict) -> tuple[int, dict[str, int]]:
    """Score a blocked issue. Returns (total_score, breakdown).

    Breakdown keys: priority, type, tags, duration, blockers.
    """
    breakdown: dict[str, int] = {}

    # Priority
    priority = _get_priority_name(issue).lower()
    breakdown["priority"] = PRIORITY_SCORES.get(priority, 0)

    # Type
    issue_type = _get_type_name(issue).lower()
    breakdown["type"] = TYPE_BONUSES.get(issue_type, 0)

    # Tags
    tag_bonus = 0
    for tag in issue.get("tags", []):
        tag_name = tag.get("name", "").lower()
        tag_bonus += TAG_BONUSES.get(tag_name, 0)
    breakdown["tags"] = tag_bonus

    # Duration (how long blocked)
    days = _days_since_update(issue)
    duration = 0
    for threshold_days, bonus, _label in BLOCKED_DURATION_THRESHOLDS:
        if days > threshold_days:
            duration = bonus
            break
    breakdown["duration"] = duration

    # Blockers
    blocker_count = _count_blockers(issue)
    breakdown["blockers"] = min(blocker_count * BLOCKER_BONUS, BLOCKER_CAP)

    total = sum(breakdown.values())
    return total, breakdown


def format_score_breakdown(breakdown: dict[str, int]) -> str:
    """Format breakdown as compact string: 'priority=80 type=20 blockers=50'."""
    return " ".join(
        f"{k}={v}" for k, v in breakdown.items() if v > 0
    )
