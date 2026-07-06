"""YouTrack COMMAND construction & application — the single home for the
invariants that were previously scattered across create_issue's closures.

YouTrack has two grammars that look alike but differ on one load-bearing
point (conflating them caused the ADR-016 regression):

- **Search queries** (`/api/issues?query=…`): `{braces}` are REQUIRED around
  multi-word values (`State: {In Progress}`).
- **Commands** (`/api/commands`): braces are REJECTED (400), for multi-word
  AND single-word values alike; the parser matches enum values greedily, so
  `Status New Employee` is the correct form (ADR-019, verified live).

In this module braces are an INPUT-ONLY grouping convention (so a multi-word
value can be captured as one token); they never survive into anything sent
to `/api/commands`.

Field NAMES can themselves be multi-word and emoji-decorated ("Dev
Estimation", "Evaluation time 🕙"), which no context-free regex can split
correctly — `split_command` uses the project's real field names as clause
boundaries instead (ADR-021).
"""
import re

import httpx

# Legacy context-free splitter: assumes one-token field names. Kept as the
# fallback when the project's field list is unavailable.
CMD_FIELD_RE = re.compile(r"(\S+)\s+\{([^}]+)\}|(\S+)\s+(\S+)")
# Command verbs that are not field names (callers may want to filter these).
CMD_KEYWORDS = frozenset({"tag", "untag", "remove", "add", "for", "star", "unstar"})
# Tokenizer for the field-aware split: {…} groups are atomic value tokens.
CMD_TOKEN_RE = re.compile(r"\{([^}]*)\}|(\S+)")

_COMMANDS_API = "/api/commands"


def strip_braces(command: str) -> str:
    """Remove the input-only brace grouping before a command reaches YT."""
    return command.replace("{", "").replace("}", "")


def cmd_error_text(e: Exception) -> str:
    """Concise, URL-free text for a failed command.

    ValueErrors from client._handle_error (including YouTrackPermissionError)
    already carry clean, truncated text. Anything else — 5xx, or a directly
    raised httpx.HTTPStatusError — would stringify with the full request URL
    (leaks the instance host), so reduce it to status + short reason.
    """
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        reason = "insufficient permissions" if code in (401, 403) else "request failed"
        return f"HTTP {code} ({reason})"
    return str(e)


async def get_project_field_names(client, project_id: str) -> list[str]:
    """Custom-field names for a project (best-effort, [] on any failure).

    GET /api/admin/projects is permission-filtered, not admin-gated
    (ADR-018), so this works for regular users too.
    """
    try:
        fields = await client.get(
            f"/api/admin/projects/{project_id}/customFields",
            params={"fields": "field(name)"},
        )
        return [
            n for f in fields
            if (n := (f.get("field") or {}).get("name", "").strip())
        ]
    except (httpx.HTTPStatusError, ValueError, KeyError):
        return []


def regex_split(command: str) -> list[str]:
    """Context-free `name value` pair split (braces stripped). Fallback only."""
    return [
        f"{m.group(1) or m.group(3)} {m.group(2) or m.group(4)}"
        for m in CMD_FIELD_RE.finditer(command)
    ]


def split_command(command: str, field_names: list[str]) -> list[str]:
    """Split a multi-field command into per-field clauses using the project's
    REAL field names as boundaries.

    Handles what the regex split cannot: multi-word and emoji field names
    ("Evaluation time 🕙 3d" stays one clause instead of splitting into
    "Evaluation time" + "🕙 3d"). A {braced} token is always a value, never a
    field boundary, and braces never survive into the output.

    A field-name match only opens a NEW clause if the current clause already
    has a value, so an unbraced value that happens to start with another
    field's name ("Type Product task" where "Product" is also a field) is not
    mis-split. Returns [] when no known field name matches (caller falls back
    to regex_split).
    """
    tokens: list[tuple[str, bool]] = []  # (text, is_braced_value)
    for m in CMD_TOKEN_RE.finditer(command):
        if m.group(1) is not None:
            if m.group(1).strip():
                tokens.append((m.group(1).strip(), True))
        else:
            tokens.append((m.group(2), False))
    # Longest name (in words) first so "QA Estimation" wins over any
    # single-word prefix of it.
    names = sorted(
        {n for n in field_names if n},
        key=lambda n: -len(n.split()),
    )
    name_parts = [(n, [p.lower() for p in n.split()]) for n in names]

    clauses: list[str] = []
    cur_field: str | None = None
    cur_value: list[str] = []

    def flush():
        nonlocal cur_field, cur_value
        if cur_field is not None and cur_value:
            clauses.append(f"{cur_field} {' '.join(cur_value)}")
        cur_field, cur_value = None, []

    i = 0
    while i < len(tokens):
        matched_len = 0
        matched_name = ""
        if not tokens[i][1] and (cur_field is None or cur_value):
            for name, parts in name_parts:
                k = len(parts)
                seg = tokens[i:i + k]
                if len(seg) == k and all(not braced for _, braced in seg) and \
                        [t.lower() for t, _ in seg] == parts:
                    matched_len, matched_name = k, name
                    break
        if matched_len:
            flush()
            cur_field = matched_name
            i += matched_len
        else:
            cur_value.append(tokens[i][0])
            i += 1
    flush()
    return clauses


def split_field_clauses(command: str, field_names: list[str]) -> list[str]:
    """Field-aware split with the regex fallback baked in."""
    return split_command(command, field_names) or regex_split(command)


def make_field_names_getter(client, project_id: str):
    """Async memoized getter — fetch the field list at most once per flow
    (e.g. shared by create_issue's direct and draft paths)."""
    cache: list[list[str]] = []

    async def get() -> list[str]:
        if not cache:
            cache.append(await get_project_field_names(client, project_id))
        return cache[0]

    return get


async def apply_field_commands(client, issue_ref: dict, command: str,
                               get_field_names) -> list[str]:
    """Apply a (possibly multi-field) command to one issue, resiliently.

    Strategy (ADR-019/021): try the whole command bare in one call; on
    failure split per-field using the project's real field names (regex
    fallback) and apply clause-by-clause; rejoin any failed clauses for one
    last combined attempt. Returns human-readable failure strings ("" == all
    applied); never raises for command-level errors.
    """
    failed: list[str] = []
    if not command:
        return failed
    whole = strip_braces(command)
    try:
        await client.post(
            _COMMANDS_API, json={"query": whole, "issues": [issue_ref]},
        )
        return failed  # full command worked
    except (httpx.HTTPStatusError, ValueError):
        pass  # fall through to split (400 parse *and* 401/403 perm)
    clauses = split_field_clauses(command, await get_field_names())
    split_failed: list[str] = []
    for cmd in clauses:
        try:
            await client.post(
                _COMMANDS_API, json={"query": cmd, "issues": [issue_ref]},
            )
        except (httpx.HTTPStatusError, ValueError):
            split_failed.append(cmd)
    if split_failed:
        # Rejoin failed splits and retry as one command (multi-field
        # combinations occasionally parse together but not alone).
        rejoined = " ".join(split_failed)
        try:
            await client.post(
                _COMMANDS_API, json={"query": rejoined, "issues": [issue_ref]},
            )
        except (httpx.HTTPStatusError, ValueError) as cmd_err:
            failed.append(f"`{rejoined}`: {cmd_error_text(cmd_err)}")
    return failed
