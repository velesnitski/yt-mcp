"""Markdown renderers for the three deadline tools."""

from collections import Counter

from yt_mcp.formatters import compact_lines
from yt_mcp.tools.deadlines.parser import _format_date


def _bucket_emoji(c: str) -> str:
    return {
        "compliant_strict": "✓",
        "compliant_loose": "~",
        "unauthorized": "✗",
        "approver_unknown": "?",
        "pre_policy": "·",
        "informational": "·",
    }.get(c, "?")


def render_audit(rows, operator, query, strict, source_file, coverage_missing,
                  policy_effective_set: bool) -> str:
    counts = Counter(r["classification"] for r in rows)
    lines = [
        "## Deadline audit",
        f"**Operator:** {operator} | **Strict:** {strict} | "
        f"**Config:** {source_file or '(none)'}",
        f"**Query:** `{query}`",
        f"**Shifts found:** {len(rows)}",
    ]
    if not policy_effective_set:
        lines.append(
            "⚠ No `policy_effective_date` set in ~/.yt-mcp/policy.json — "
            "pre-policy filtering disabled (all shifts in scope)."
        )
    lines.extend(["", "### Rollup"])
    for bucket in (
        "compliant_strict", "compliant_loose", "unauthorized",
        "approver_unknown", "pre_policy", "informational",
    ):
        if counts.get(bucket):
            lines.append(f"- {_bucket_emoji(bucket)} **{bucket}**: {counts[bucket]}")
    lines.append("")
    for cls_filter in ("unauthorized", "approver_unknown", "compliant_loose"):
        section = [r for r in rows if r["classification"] == cls_filter]
        if not section:
            continue
        lines.append(f"### {cls_filter} ({len(section)})")
        for r in section[:50]:
            lines.append(
                f"- **{r['issue']}** {_format_date(r['old'])} → {_format_date(r['new'])} "
                f"by **{r['author']}** | assignee: {r['assignee']} | "
                f"activity: `{r['activity_id']}` | "
                f"approvers: {sorted(r['approvers']) or '(none)'}"
            )
            for ev in r["evidence"][:2]:
                lines.append(f"  - _{ev}_")
        if len(section) > 50:
            lines.append(f"_(+{len(section) - 50} more)_")
        lines.append("")
    if coverage_missing:
        lines.append("### Coverage gaps")
        lines.append(
            f"**{len(coverage_missing)} assignees** have no approver mapping — "
            "their shifts cannot be classified. Run `suggest_managers` then update "
            "`managers.json`:"
        )
        for u in sorted(coverage_missing)[:20]:
            lines.append(f"- {u}")
        if len(coverage_missing) > 20:
            lines.append(f"_(+{len(coverage_missing) - 20} more)_")
    return compact_lines(lines)


def render_scorecard(per_user, per_user_details, quarter, operator, strict,
                      source_file, coverage_missing, fallback_query_used: bool,
                      policy_effective_set: bool) -> str:
    lines = [
        f"## Deadline scorecard — {quarter}",
        f"**Operator:** {operator} | **Strict:** {strict} | "
        f"**Config:** {source_file or '(none)'}",
    ]
    if fallback_query_used:
        lines.append(
            "⚠ YouTrack rejected the `due date:` query clause — fell back to "
            "`updated:` only. Issues whose deadline is in this quarter but "
            "weren't updated recently may be under-counted."
        )
    if not policy_effective_set:
        lines.append(
            "⚠ No `policy_effective_date` set — old shifts may be counted."
        )
    lines.append("")
    if not per_user:
        lines.append("_No deadline activity in scope._")
        return compact_lines(lines)

    def _penalty(u: str) -> int:
        c = per_user[u]
        return c.get("unauthorized", 0) + c.get("missed_no_extension", 0) + c.get("missed_after_extension", 0)

    for user in sorted(per_user, key=lambda u: -_penalty(u)):
        c = per_user[user]
        lines.append(f"### {user}")
        lines.append(
            f"- On-time: {c.get('compliant_strict', 0)} strict, {c.get('compliant_loose', 0)} loose"
        )
        if c.get("unauthorized"):
            lines.append(f"- ✗ **Unauthorized shifts:** {c['unauthorized']}")
        if c.get("missed_no_extension"):
            lines.append(f"- ✗ **Missed (no approved extension):** {c['missed_no_extension']}")
        if c.get("missed_after_extension"):
            lines.append(f"- ⚠ **Missed after extension:** {c['missed_after_extension']}")
        if c.get("approver_unknown"):
            lines.append(f"- ? **Approver unknown:** {c['approver_unknown']} (mapping gap, not penalty)")
        if c.get("pre_policy"):
            lines.append(f"- · Pre-policy shifts (informational): {c['pre_policy']}")
        details = per_user_details.get(user, [])
        if details:
            lines.append("")
            lines.extend(details[:20])
            if len(details) > 20:
                lines.append(f"_(+{len(details) - 20} more)_")
        lines.append("")
    if coverage_missing:
        lines.append("### Coverage gaps")
        lines.append(
            f"_{len(coverage_missing)} assignees have no approver mapping. "
            "Results above may understate penalties or misclassify as 'approver_unknown'._"
        )
    return compact_lines(lines)


def render_suggestion(suggestion, operator, lookback_days, written_path, pms) -> str:
    entries = {k: v for k, v in suggestion.items() if k != "_metadata"}
    committed = sum(1 for v in entries.values() if isinstance(v, dict) and v.get("primary"))
    flagged = sum(1 for v in entries.values() if isinstance(v, dict) and v.get("manual_review"))
    lines = [
        "## Manager suggester",
        f"**Operator:** {operator} | **Lookback:** {lookback_days}d",
        f"**Targets analyzed:** {len(entries)}",
        f"**PMs detected (excluded as approvers):** {len(pms)}"
        + (f": {', '.join(sorted(pms))}" if pms else ""),
        f"**Committed primaries:** {committed} | **Flagged for review:** {flagged}",
        f"**File:** {written_path or '(not written)'}",
        "",
        "### Sample entries",
    ]
    for target, entry in list(entries.items())[:10]:
        if not isinstance(entry, dict):
            continue
        primary = entry.get("primary") or "?"
        also = entry.get("also_accept") or []
        ev = entry.get("evidence", [])
        marker = "?" if entry.get("manual_review") else "✓"
        lines.append(f"- {marker} **{target}** → `{primary}`{f' (also: {also})' if also else ''}")
        for e in ev[:2]:
            lines.append(f"  - _{e}_")
    lines.extend([
        "",
        "Review the file, hand-correct flagged entries, copy to `managers.json`.",
    ])
    return compact_lines(lines)
