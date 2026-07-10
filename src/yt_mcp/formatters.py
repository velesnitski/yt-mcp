import os
import re

_ISSUE_URL_RE = re.compile(r"/issue/([A-Za-z]+-\d+)")

# Compact mode: strips markdown formatting for token savings (~60%)
# Set YOUTRACK_COMPACT=1 to enable
COMPACT = os.environ.get("YOUTRACK_COMPACT", "").lower() in ("1", "true", "yes")


def escape_query_value(value: str) -> str:
    """Escape a value for safe use in YouTrack search queries."""
    # Remove braces and backslashes that could break query syntax
    return value.replace("\\", "").replace("{", "").replace("}", "")


_OR_SPLIT_RE = re.compile(r"\s+OR\s+", re.IGNORECASE)
_PREFIX_VALUE_RE = re.compile(r"^\s*(\w+)\s*:\s*(.+?)\s*$")


def rewrite_or_clauses(query: str) -> tuple[str, list[str]]:
    """Rewrite `<prefix>: x OR <prefix>: y` → `<prefix>: x, y`.

    YouTrack rejects OR-joined same-prefix clauses with a generic 400
    'Can't parse search query'. The correct idiom is the comma-list form
    `<prefix>: x, y`. This helper detects the simple-clause shape and
    auto-rewrites — a footgun avoidance pass that saves the caller a
    round-trip and a confusing error.

    Conservative bail: if the query contains braces, quotes, or parens,
    leave it alone. Brace-wrapped values (`{For Review}`) and quoted
    strings may contain literal " OR " text we shouldn't touch; parens
    indicate nested groups whose semantics auto-merge could change.

    Returns (new_query, [rewrite_descriptions]). Empty list means no
    change. The descriptions are for logging — never user-visible.
    """
    if not query or any(c in query for c in "(){}\""):
        return query, []

    segments = _OR_SPLIT_RE.split(query)
    if len(segments) < 2:
        return query, []

    parsed: list[tuple[str, str, str]] = []  # (prefix_lower, prefix_orig, value)
    for seg in segments:
        m = _PREFIX_VALUE_RE.match(seg)
        if not m:
            return query, []  # one segment isn't a simple `prefix: value` — bail
        prefix_orig = m.group(1)
        parsed.append((prefix_orig.lower(), prefix_orig, m.group(2)))

    # All segments must share the same prefix for a safe merge.
    head_prefix = parsed[0][0]
    if not all(p[0] == head_prefix for p in parsed):
        return query, []

    prefix = parsed[0][1]  # preserve original-case prefix from first clause
    values = [p[2] for p in parsed]
    new_query = f"{prefix}: " + ", ".join(values)
    return new_query, [f"merged {len(segments)} `{prefix}:` OR-clauses into comma-list"]


def build_state_clause(states: list[str]) -> str:
    """Build a `State:` clause in YT's comma-list idiom.

    YouTrack accepts `State: {A}, {B}, {C}` (comma-list) but rejects
    `(State: {A} or State: {B})` (OR-joined repeated-field) on many
    versions/projects — surfaces as 400 "Can't parse search query".

    Empty input returns empty string; the caller can compose conditionally.
    """
    if not states:
        return ""
    return "State: " + ", ".join(f"{{{s}}}" for s in states)


def normalize_issue(data: dict, include_comments: bool = True) -> dict:
    """Flatten a YT issue response into a JSON-friendly dict.

    YT's native shape has `customFields` as a list of `{name, value}` pairs
    and tags/links as nested objects — readable, but every consumer has to
    walk the same boilerplate. This helper produces a clean dict that
    matches the shape used by other tools' `format="json"` outputs (pulse,
    handoffs):

      {
        "id", "summary", "description",
        "state", "priority",
        "assignee", "assignee_login",
        "created", "updated", "resolved",
        "tags": [str, ...],
        "custom_fields": {field_name: value, ...},
        "links": [{id, summary, state, link_type, direction}, ...],
        "comments": [{id, text, author, author_login, created}, ...],
      }
    """
    custom_fields: dict[str, object] = {}
    for cf in data.get("customFields", []) or []:
        name = cf.get("name")
        if not name:
            continue
        val = cf.get("value")
        if isinstance(val, dict):
            custom_fields[name] = (
                val.get("name") or val.get("presentation")
                or val.get("text") or val.get("login")
            )
        elif isinstance(val, list):
            custom_fields[name] = [
                v.get("name") or v.get("presentation") or v.get("text") or v.get("login")
                for v in val if isinstance(v, dict)
            ]
        else:
            custom_fields[name] = val

    tags = [t.get("name", "") for t in (data.get("tags") or []) if t.get("name")]

    out: dict = {
        "id": data.get("idReadable", ""),
        "summary": data.get("summary", "") or "",
        "description": data.get("description") or "",
        "state": _resolve_state(data),
        "priority": _resolve_priority(data),
        "assignee": _resolve_assignee(data),
        "assignee_login": _resolve_assignee_login(data),
        "created": data.get("created"),
        "updated": data.get("updated"),
        "resolved": data.get("resolved"),
        "tags": tags,
        "custom_fields": custom_fields,
    }

    links: list[dict] = []
    for link in data.get("links", []) or []:
        link_type = (link.get("linkType") or {}).get("name", "")
        direction = link.get("direction", "")
        for linked in link.get("issues", []) or []:
            ls = linked.get("state")
            state = ls.get("name", "") if isinstance(ls, dict) else ""
            links.append({
                "id": linked.get("idReadable", ""),
                "summary": linked.get("summary", "") or "",
                "state": state,
                "link_type": link_type,
                "direction": direction,
            })
    out["links"] = links

    if include_comments and data.get("comments") is not None:
        out["comments"] = [
            {
                "id": c.get("id", ""),
                "text": c.get("text", "") or "",
                "author": (c.get("author") or {}).get("name", ""),
                "author_login": (c.get("author") or {}).get("login"),
                "created": c.get("created"),
                **({"repeats": n} if (n := c.get("_repeats", 1)) > 1 else {}),
            }
            for c in dedupe_comments(data.get("comments") or [])
        ]
    return out


def dedupe_comments(comments: list[dict]) -> list[dict]:
    """Collapse comments with identical (author, text) into one entry.

    Workflow bots post the same nag many times ("log your time" ×4) — pure
    token noise on issue reads. First occurrence keeps its position/id;
    duplicates only bump a `_repeats` counter (renderers show "(×N)").
    Distinct texts or authors are never merged.
    """
    seen: dict[tuple, dict] = {}
    out: list[dict] = []
    for c in comments:
        key = ((c.get("author") or {}).get("name", ""), (c.get("text") or "").strip())
        if key in seen and key[1]:
            seen[key]["_repeats"] = seen[key].get("_repeats", 1) + 1
            continue
        kept = dict(c)
        seen[key] = kept
        out.append(kept)
    return out


def build_absolute_date_clause(days_ago: int, now_ms: int) -> str:
    """Absolute-date range `YYYY-MM-DD .. YYYY-MM-DD` for `updated:` /
    `created:` / `resolved:` filters. Relative bounds like `-30d` or
    `{minus 14d}` are version-dependent in YT — absolute dates work
    everywhere."""
    from datetime import datetime, timedelta, timezone
    end_dt = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
    start_dt = end_dt - timedelta(days=days_ago)
    return f"{start_dt.strftime('%Y-%m-%d')} .. {end_dt.strftime('%Y-%m-%d')}"


def compact_lines(lines: list[str]) -> str:
    """Join lines, stripping markdown if compact mode is on."""
    text = "\n".join(lines)
    if not COMPACT:
        return text
    # Strip markdown formatting
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"^#+\s+", "", text, flags=re.MULTILINE)  # ## Headers → plain
    text = re.sub(r"^- ", "", text, flags=re.MULTILINE)  # bullet points
    text = re.sub(r"_([^_]+)_", r"\1", text)  # _italic_
    text = re.sub(r"\n{3,}", "\n\n", text)  # collapse blank lines
    return text.strip()


def parse_issue_id(issue_id: str) -> str:
    """Extract issue ID from a string that may be an ID or a YouTrack URL.

    Accepts:
        - 'PROJ-123'
        - 'https://company.youtrack.cloud/issue/PROJ-123/some-slug'
    """
    match = _ISSUE_URL_RE.search(issue_id)
    if match:
        return match.group(1)
    return issue_id.strip()


def _get_custom_field(issue: dict, field_name: str) -> str | None:
    """Extract a custom field value by name from an issue's customFields array.

    Handles SingleEnum (dict with 'name'), MultiEnum/User list (list of dicts),
    and plain string values. Returns the display string or None if not found.
    """
    for cf in issue.get("customFields", []):
        if cf.get("name") == field_name:
            val = cf.get("value")
            if val is None:
                return None
            if isinstance(val, dict):
                return val.get("name")
            if isinstance(val, list):
                names = [v.get("name", "") for v in val if isinstance(v, dict) and v.get("name")]
                return ", ".join(names) if names else None
            if isinstance(val, str):
                return val
    return None


def get_product(issue: dict) -> str:
    """Extract Product field value from an issue's custom fields."""
    return _get_custom_field(issue, "Product") or ""


def _resolve_state(issue: dict) -> str:
    """Get state name from top-level field or customFields fallback."""
    state = issue.get("state")
    if state and isinstance(state, dict):
        name = state.get("name")
        if name:
            return name
    return _get_custom_field(issue, "State") or "Unknown"


def _resolve_priority(issue: dict) -> str:
    """Get priority name from top-level field or customFields fallback."""
    priority = issue.get("priority")
    if priority and isinstance(priority, dict):
        name = priority.get("name")
        if name:
            return name
    return _get_custom_field(issue, "Priority") or "?"


def _resolve_assignee(issue: dict) -> str:
    """Get assignee name from top-level field or customFields fallback."""
    assignee = issue.get("assignee")
    if assignee and isinstance(assignee, dict):
        name = assignee.get("name")
        if name:
            return name
    cf_assignee = _get_custom_field(issue, "Assignee")
    return cf_assignee or "Unassigned"


def _resolve_assignee_login(issue: dict) -> str | None:
    """Get assignee login (YouTrack username) for live `Assignee:` query
    filters. YouTrack's `Assignee:` syntax requires the login string, NOT
    the display name. Returns None when no login is resolvable so callers
    can fall back to ID-list queries."""
    a = issue.get("assignee")
    if isinstance(a, dict) and a.get("login"):
        return a["login"]
    for cf in issue.get("customFields", []):
        if cf.get("name") == "Assignee":
            v = cf.get("value")
            if isinstance(v, dict) and v.get("login"):
                return v["login"]
            if isinstance(v, list) and v:
                first = v[0]
                if isinstance(first, dict) and first.get("login"):
                    return first["login"]
    return None


def format_issue_list(issues: list) -> str:
    if not issues:
        return "No issues found."
    lines = []
    for issue in issues:
        assignee_name = _resolve_assignee(issue)
        state_name = _resolve_state(issue)
        product = get_product(issue)
        if COMPACT:
            prod_str = f"|{product}" if product else ""
            lines.append(
                f"{issue.get('idReadable', '?')}|{state_name}{prod_str}|"
                f"{issue.get('summary') or ''}|{assignee_name}"
            )
        else:
            product_str = f" ({product})" if product else ""
            lines.append(
                f"- **{issue.get('idReadable', '?')}** [{state_name}]{product_str} "
                f"{issue.get('summary') or 'No summary'} → {assignee_name}"
            )
    return "\n".join(lines)


_DESC_TRUNCATE_LIMIT = 500


def _truncate_desc(desc: str, limit: int = _DESC_TRUNCATE_LIMIT) -> str:
    """Compact-mode description truncation that ANNOUNCES itself.

    A bare `desc[:limit]` read as if the description simply ended, and could
    sever a markdown link mid-URL (e.g. `[design](https://figma.com/fi`). So:
    cut on the last whitespace within the window — which keeps a straddling
    link whole (a link has no internal spaces, so the boundary lands before
    it) — then append a marker with the true length so the reader knows to
    fetch the full text via `format=json`.
    """
    if len(desc) <= limit:
        return desc
    window = desc[:limit]
    boundary = max(window.rfind("\n"), window.rfind(" "))
    # Only back up to whitespace when it's near the edge; a long unbroken
    # token (a bare URL filling the window) hard-cuts rather than gutting it.
    cut = window[:boundary] if boundary >= limit - 80 else window
    return (
        f"{cut.rstrip()}\n"
        f"_… truncated ({len(desc)} chars total) — use format=json for full_"
    )


def format_issue_detail(data: dict) -> str:
    state_name = _resolve_state(data)
    priority_name = _resolve_priority(data)
    assignee_name = _resolve_assignee(data)
    product = get_product(data)
    # `or []` not `.get(default)` — these keys can come back present-but-null.
    tags = data.get("tags") or []
    desc = data.get("description")
    links = data.get("links") or []

    if COMPACT:
        iid = data.get("idReadable", "?")
        parts = [f"{iid}|{state_name}|{priority_name}|{assignee_name}"]
        if product:
            parts[0] += f"|{product}"
        # `or ""` (not a .get default) — YouTrack sends the key present with a
        # null value for empty fields, so the default never fires. A null
        # summary here would crash the join below.
        parts.append(data.get("summary") or "")
        if tags:
            parts.append(f"Tags:{','.join(t.get('name', '') for t in tags)}")
        if desc:
            # Compact mode truncates the description — self-announcing and
            # link-safe (ADR-028), so it never masquerades as "the end" or
            # severs a markdown link (e.g. a Figma URL) mid-string.
            parts.append(_truncate_desc(desc))
        for link in links:
            lt = (link.get("linkType") or {}).get("name", "?")
            d = link.get("direction", "?")
            for linked in link.get("issues") or []:
                ls = linked.get("state")
                st = ls.get("name", "") if isinstance(ls, dict) else ""
                parts.append(f"{lt}({d}):{linked.get('idReadable', '?')}[{st}]")
        comments = dedupe_comments(data.get("comments") or [])
        if comments:
            for c in comments:
                author = (c.get("author") or {}).get("name", "?")
                # null text → None[:200] crash (the production bug). Coerce.
                text = (c.get("text") or "")[:200]
                rep = f"(x{c['_repeats']})" if c.get("_repeats", 1) > 1 else ""
                parts.append(f"@{author}{rep}:{text}")
        return "\n".join(parts)

    parts = [
        f"# {data.get('idReadable', '?')}: {data.get('summary') or ''}",
        "",
        f"**State:** {state_name}",
        f"**Priority:** {priority_name}",
        f"**Assignee:** {assignee_name}",
    ]

    if product:
        parts.append(f"**Product:** {product}")

    if tags:
        parts.append(f"**Tags:** {', '.join(t.get('name', '') for t in tags)}")

    if desc:
        parts.extend(["", "## Description", desc])

    if links:
        has_linked = False
        link_lines = []
        for link in links:
            link_type = (link.get("linkType") or {}).get("name", "?")
            direction = link.get("direction", "?")
            for linked in link.get("issues") or []:
                linked_state = ""
                ls = linked.get("state")
                if ls and isinstance(ls, dict) and ls.get("name"):
                    linked_state = ls["name"]
                else:
                    linked_state = _get_custom_field(linked, "State") or ""
                state_str = f" [{linked_state}]" if linked_state else ""
                link_lines.append(
                    f"- **{link_type}** ({direction}): "
                    f"{linked.get('idReadable', '?')}{state_str} — {linked.get('summary') or ''}"
                )
                has_linked = True
        if has_linked:
            parts.append("")
            parts.append("## Links")
            parts.extend(link_lines)

    raw_comments = data.get("comments") or []
    if raw_comments:
        comments = dedupe_comments(raw_comments)
        parts.extend(["", f"## Comments ({len(raw_comments)})"])
        for c in comments:
            author = (c.get("author") or {}).get("name", "Unknown")
            rep = f" _(×{c['_repeats']})_" if c.get("_repeats", 1) > 1 else ""
            parts.append(f"**{author}:**{rep} {c.get('text') or ''}")
            parts.append("")

    return "\n".join(parts)


# --- Shared constants for dashboard/monitoring tools ---

ISSUE_FIELDS = (
    "idReadable,summary,updated,created,state(name),priority(name),"
    "assignee(name),tags(name),"
    "customFields(name,value(name)),"
    "links(direction,linkType(name),issues(idReadable))"
)

ACTIVE_STATES = frozenset({"in progress", "submitted", "in review", "ready for test"})


def compile_exclude_patterns(exclude_patterns: str) -> list[re.Pattern]:
    """Compile comma-separated regex patterns for issue exclusion."""
    if not exclude_patterns:
        return []
    return [
        re.compile(p.strip(), re.IGNORECASE)
        for p in exclude_patterns.split(",")
        if p.strip()
    ]


def should_exclude(issue: dict, patterns: list[re.Pattern]) -> bool:
    """Check if issue summary matches any exclusion pattern."""
    summary = issue.get("summary", "")
    return any(p.search(summary) for p in patterns)


def format_value(val) -> str:
    if val is None:
        return "(empty)"
    if isinstance(val, list):
        names = [v.get("name", "") or v.get("text", "") for v in val]
        return ", ".join(names) if names else "(empty)"
    if isinstance(val, str):
        return val[:200] if len(val) > 200 else val
    return str(val)
