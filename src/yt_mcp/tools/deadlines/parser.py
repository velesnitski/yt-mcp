"""Pure functions for deadline parsing and shift classification."""

import re
from datetime import datetime, timezone
from typing import Any


_DEADLINE_FIELD_PATTERNS = (
    re.compile(r"^(deadline|due\s*date|due|completion\s*date)$", re.IGNORECASE),
    re.compile(r"^(дедлайн|срок|до|дата\s*выполнения)$", re.IGNORECASE),
)

_APPROVAL_KEYWORDS = (
    "approve", "approved", "ok", "okay", "agreed", "extend", "extended",
    "confirm", "confirmed", "согласен", "одобрено", "ок",
)

_DEFAULT_STANDUP_PATTERNS = (
    r"(?i)devops\s+daily",
    r"(?i)\bdaily\b",
    r"(?i)\bstandup\b",
    r"(?i)\bдейли\b",
    r"(?i)\bстендап\b",
    r"(?i)решение\s+текущих\s+проблем",
)

_DONE_STATES = frozenset({
    "done", "closed", "resolved", "fixed", "completed", "released", "verified",
})

_APPROVAL_WINDOW_BEFORE_SEC = 14 * 86400
_APPROVAL_WINDOW_AFTER_SEC = 24 * 3600


def _is_deadline_field(name: str) -> bool:
    return bool(name) and any(p.match(name.strip()) for p in _DEADLINE_FIELD_PATTERNS)


def _extract_deadline_ts(val: Any) -> int | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, dict):
        pres = val.get("presentation")
        if pres:
            try:
                return int(
                    datetime.strptime(str(pres), "%Y-%m-%d")
                    .replace(tzinfo=timezone.utc).timestamp() * 1000
                )
            except ValueError:
                pass
    return None


def _extract_activity_date(item: Any) -> int | None:
    if not item:
        return None
    if isinstance(item, list):
        if not item:
            return None
        item = item[0]
    if isinstance(item, dict):
        for key in ("presentation", "name", "text"):
            v = item.get(key)
            if v:
                try:
                    return int(
                        datetime.strptime(str(v), "%Y-%m-%d")
                        .replace(tzinfo=timezone.utc).timestamp() * 1000
                    )
                except ValueError:
                    continue
    if isinstance(item, (int, float)):
        return int(item)
    return None


def _format_date(ms: int | None) -> str:
    if not ms:
        return "(none)"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _compile_standup_patterns(policy: dict) -> list[re.Pattern]:
    patterns = policy.get("standup_patterns") or list(_DEFAULT_STANDUP_PATTERNS)
    return [re.compile(p) for p in patterns]


def _is_standup(summary: str, patterns: list[re.Pattern]) -> bool:
    return any(p.search(summary or "") for p in patterns)


def _classify_shift(
    *,
    shift_ts: int,
    shift_author: str,
    old_ms: int | None,
    new_ms: int | None,
    approvers: set[str],
    manual_review: bool,
    comments: list[dict],
    strict: bool,
    policy_effective_ms: int,
) -> dict:
    """Classify a Due-Date shift into a compliance bucket with evidence.

    Returns {"classification": <bucket>, "evidence": [str, ...]}.
    Buckets: compliant_strict, compliant_loose, unauthorized,
    approver_unknown, pre_policy, informational.
    """
    if shift_ts < policy_effective_ms:
        return {"classification": "pre_policy", "evidence": ["before policy effective date"]}
    if old_ms is None or (new_ms is not None and new_ms <= old_ms):
        return {"classification": "informational", "evidence": ["first-time set or earlier date"]}
    if not approvers:
        return {"classification": "approver_unknown", "evidence": ["no approver mapping for assignee"]}
    if manual_review:
        return {"classification": "approver_unknown", "evidence": ["mapping flagged manual_review"]}
    if shift_author in approvers:
        return {
            "classification": "compliant_strict",
            "evidence": [f"shift author {shift_author} is an approver"],
        }

    win_start = shift_ts - _APPROVAL_WINDOW_BEFORE_SEC * 1000
    win_end = shift_ts + _APPROVAL_WINDOW_AFTER_SEC * 1000
    new_date_str = _format_date(new_ms)
    strict_ev: list[str] = []
    loose_ev: list[str] = []
    for c in comments:
        c_ts = c.get("created") or c.get("ts") or 0
        if c_ts < win_start or c_ts > win_end:
            continue
        author = c.get("author") or {}
        c_author = author.get("login") or author.get("name") or ""
        if c_author not in approvers:
            continue
        c_text = (c.get("text") or "").lower()
        c_id = c.get("id", "?")
        has_kw = any(kw in c_text for kw in _APPROVAL_KEYWORDS)
        has_date = bool(new_date_str) and new_date_str in c_text
        if has_kw and has_date:
            strict_ev.append(f"comment {c_id} by {c_author}: keyword + new date")
        elif has_kw:
            # BUG-FIX: was `has_kw or c_ts <= shift_ts` — any in-window comment
            # by an approver was incorrectly classified loose, regardless of
            # content. Require an approval keyword.
            loose_ev.append(f"comment {c_id} by {c_author}: keyword only")

    if strict_ev:
        return {"classification": "compliant_strict", "evidence": strict_ev}
    if loose_ev and not strict:
        return {"classification": "compliant_loose", "evidence": loose_ev}
    return {
        "classification": "unauthorized",
        "evidence": [f"no approval from {sorted(approvers)} in window"],
    }
