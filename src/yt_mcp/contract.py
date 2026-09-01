"""The YouTrack API contract: query construction and response-shape readers.

One auditable home for the logic that encodes YouTrack's documented
quirks (`docs/YT_QUIRKS.md`, Q1-Q17). Every function here exists because
the obvious form is silently wrong against a live instance: a query that
400s, or worse, one that parses and returns nothing.

Deliberately **stdlib-only and synchronous** — no httpx, no mcp SDK, no
async. That is a load-bearing property, not an accident: this module is the
importable surface for consumers that cannot take the server's dependency
tree (the reporting pipeline), so the quirks are fixed once instead of
mirrored by hand. Anything needing a live client belongs in `client.py`;
anything MCP-shaped belongs in `tools/`.

Guard convention: each quirk's regression test lives beside the function
that encodes it. A quirk without a guard is a regression waiting to happen.
"""

import re
from datetime import datetime, timedelta, timezone

# --- Q1/Q2: date-range queries ---------------------------------------------
# `resolved:` is an alias that stopped accepting ranges after a 2026-07 Cloud
# upgrade: a spaced range 400s, and an UNSPACED range parses but silently
# matches nothing — the trap variant. `resolved date:` is the canonical
# attribute. `created:`/`updated:` are unaffected (Q14) and keep working.
RESOLVED_DATE_ATTR = "resolved date:"


def build_date_range(days: int, now_ms: int | None = None) -> str:
    """`YYYY-MM-DD .. YYYY-MM-DD` for a lookback window.

    Absolute ISO dates only: the `-Nd .. *` form is rejected (the `*` upper
    bound) and bare relative offsets are version-dependent.
    """
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000) if now_ms is None else now_ms
    end = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
    start = end - timedelta(days=days)
    return f"{start.strftime('%Y-%m-%d')} .. {end.strftime('%Y-%m-%d')}"


def resolved_window_query(days: int, *rest: str, now_ms: int | None = None) -> str:
    """A resolved-in-window query with the range clause FIRST.

    Clause order is load-bearing (Q2): the parser rejects a composed query
    whose `resolved date:` range is not the leading clause. Passing the rest
    of the query through this builder makes the ordering impossible to get
    wrong at a call site.
    """
    tail = " ".join(c.strip() for c in rest if c and c.strip())
    head = f"{RESOLVED_DATE_ATTR} {build_date_range(days, now_ms)}"
    return f"{head} {tail}".strip()


# --- Q17: same-prefix OR clauses -------------------------------------------
def project_clause(projects: list[str] | str) -> str:
    """`project: A, B` — never `project: A OR project: B` (which 400s)."""
    if isinstance(projects, str):
        projects = [p.strip() for p in projects.split(",")]
    keys = [escape_query_value(p.strip()) for p in projects if p and p.strip()]
    return f"project: {', '.join(keys)}" if keys else ""


def escape_query_value(value: str) -> str:
    """Strip characters that break query syntax (braces, backslashes)."""
    return value.replace("\\", "").replace("{", "").replace("}", "")


# --- Q3: linked issues carry no top-level `state` --------------------------
def custom_field(issue: dict, name: str) -> str | None:
    """Read a custom field by exact name (emoji included — Q9)."""
    for cf in issue.get("customFields", []) or []:
        if cf.get("name") == name:
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


def linked_state(linked: dict) -> str:
    """State of a linked issue: top-level first, then the custom field.

    A `state(name)` selector on a linked issue is silently ignored by
    YouTrack — the key simply never comes back, so a naive read yields ""
    for every link while the tests mocking the imagined shape pass.
    """
    ls = linked.get("state")
    if isinstance(ls, dict) and ls.get("name"):
        return ls["name"]
    return custom_field(linked, "State") or ""


# --- Q10: service/bot activity is structural, not name-based ---------------
# Workflow accounts follow a login shape; this server stamps its own service
# comments with a text prefix. Display names are mutable and must never be
# the filter key.
SERVICE_AUTHOR_PREFIXES = ("workflow_user",)
SERVICE_TEXT_PREFIXES = ("[yt-mcp]",)


def split_service_comments(comments: list[dict]) -> tuple[list[dict], int]:
    """(human_comments, hidden_count) — peel off bot/service comments."""
    real: list[dict] = []
    hidden = 0
    for c in comments or []:
        login = (c.get("author") or {}).get("login") or ""
        text = (c.get("text") or "").lstrip()
        if login.startswith(SERVICE_AUTHOR_PREFIXES) or text.startswith(SERVICE_TEXT_PREFIXES):
            hidden += 1
        else:
            real.append(c)
    return real, hidden


# --- Q11: keyword search matches mentions, not just titles -----------------
RELEASE_RE = re.compile(r"^(\[[^\]]*\]\s*)?(rc|release)[\s_.\-]*v?(\d|lite)", re.IGNORECASE)


def is_release_ticket(summary: str) -> bool:
    """True for `Release X.Y.Z` / `RC-X.Y.Z` shapes, not mere mentions."""
    return bool(RELEASE_RE.match(summary or ""))


# --- Q16: POST responses default to {$type, id} ----------------------------
# An entity-creating POST returns ONLY $type and id unless a fields selector
# asks for more — so idReadable is absent and follow-up calls target "?".
CREATE_FIELDS = "idReadable,summary"


def with_fields(path: str, fields: str = CREATE_FIELDS) -> str:
    """Append a fields selector to an entity-creating POST path."""
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}fields={fields}"
