import json
import re

import httpx
from datetime import datetime, timezone

from yt_mcp.resolver import InstanceResolver
from yt_mcp.formatters import format_issue_list, format_issue_detail, _resolve_state, _resolve_assignee, _get_custom_field, parse_issue_id, compact_lines, normalize_issue

_CMD_FIELD_RE = re.compile(r"(\S+)\s+\{([^}]+)\}|(\S+)\s+(\S+)")
_CMD_KEYWORDS = frozenset({"tag", "untag", "remove", "add", "for", "star", "unstar"})
# Tokenizer for the field-aware split: {…} groups are atomic value tokens.
_CMD_TOKEN_RE = re.compile(r"\{([^}]*)\}|(\S+)")


async def _get_project_field_names(client, project_id: str) -> list[str]:
    """Custom-field names for a project (best-effort, [] on any failure).

    Needed because field names can be multi-word and emoji-decorated
    ("Evaluation time 🕙", "Dev Estimation", "Phase detected") — a naive
    one-token split garbles them. GET /api/admin/projects is permission-
    filtered, not admin-gated (ADR-018), so this works for regular users too.
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


def _split_command_with_field_names(command: str, field_names: list[str]) -> list[str]:
    """Split a multi-field command into per-field clauses using the project's
    REAL field names as boundaries.

    Handles what the regex split cannot: multi-word and emoji field names
    ("Evaluation time 🕙 3d" stays one clause instead of splitting into
    "Evaluation time" + "🕙 3d"). Braces remain the input-only grouping
    convention: a {braced} token is always a value, never a field boundary,
    and braces never survive into the output (ADR-019 — YT commands are bare).

    A field-name match only opens a NEW clause if the current clause already
    has a value, so an unbraced value that happens to start with another
    field's name ("Type Product task" where "Product" is also a field) is not
    mis-split. Returns [] when no known field name matches (caller falls back
    to the regex split).
    """
    tokens: list[tuple[str, bool]] = []  # (text, is_braced_value)
    for m in _CMD_TOKEN_RE.finditer(command):
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


async def _get_required_fields_info(client, project_id: str, project_short: str) -> str:
    """Best-effort required-fields hint to help the LLM fix a failed command.

    Uses /api/admin/projects/{id}/customFields — the only custom-fields
    resource (/api/projects/{id}/customFields 404s). The hint is optional: a
    non-admin user may get an empty or forbidden response there, so any failure
    (permission, parse, shape) just yields "" rather than raising.
    """
    try:
        fields = await client.get(
            f"/api/admin/projects/{project_id}/customFields",
            params={"fields": "field(name),canBeEmpty,bundle(values(name,archived))"},
        )
    except (httpx.HTTPStatusError, ValueError, KeyError):
        return ""
    lines = ["**Required fields for this project:**"]
    for f in fields:
        if f.get("canBeEmpty", True):
            continue
        name = (f.get("field") or {}).get("name", "?")
        bundle = f.get("bundle")
        if bundle and bundle.get("values"):
            vals = [v["name"] for v in bundle["values"] if not v.get("archived")]
            lines.append(f"- **{name}**: {', '.join(vals)}")
        else:
            lines.append(f"- **{name}**")
    return "\n".join(lines) if len(lines) > 1 else ""


def _cmd_error_text(e: Exception) -> str:
    """Concise, URL-free text for a failed command.

    ValueErrors from client._handle_error already carry a clean, truncated
    YouTrack message (400/404). Everything else — 401/403 permission denials
    and 5xx — surfaces via raise_for_status() as httpx.HTTPStatusError, whose
    str() embeds the full request URL (leaks the instance host). Reduce it to
    status + a short reason so failed_commands stays clean and safe.
    """
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        reason = "insufficient permissions" if code in (401, 403) else "request failed"
        return f"HTTP {code} ({reason})"
    return str(e)


def register(mcp, resolver: InstanceResolver):

    @mcp.tool()
    async def search_issues(query: str, max_results: int = 50, instance: str = "") -> str:
        """Search YouTrack issues using query syntax. Use named periods in curly braces for relative dates.

        Args:
            query: YouTrack search query
            max_results: Max results (default: 50)
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance)
        data = await client.get(
            "/api/issues",
            params={
                "query": query,
                "fields": "idReadable,summary,state(name),assignee(name),"
                "customFields(name,value(name)),created,updated",
                "$top": str(max_results),
            },
        )
        result = format_issue_list(data)

        count = len(data)
        if count == 0:
            return result
        header = f"**Found: {count} issues**"
        if count >= max_results:
            header += f" (showing first {max_results}, more may exist)"
        return f"{header}\n\n{result}"

    @mcp.tool()
    async def get_issue(
        issue_id: str,
        include_comments: bool = True,
        format: str = "report",
        fields: str = "",
        instance: str = "",
    ) -> str:
        """Get full details of a YouTrack issue.

        Default returns a markdown render (chat-friendly). `format="json"`
        returns a normalized JSON dict — flat keys for id/summary/state/
        assignee + assignee_login, a `tags` list, a `custom_fields` dict
        (custom field name → value), and a `links` list. Matches the JSON
        shape used by pulse and handoffs so consumers see one consistent
        structure across tools.

        Use `fields` to override the field selector for power callers
        who need specific YT response fields. When `fields` is set and
        `format="json"`, the raw YT response is returned unaltered (no
        normalization, full control). When `fields` is set and
        `format="report"`, the markdown render may be sparse if the
        selector is narrower than the default.

        Args:
            issue_id: Issue ID or URL.
            include_comments: Include comments in default selector (default: True).
            format: "report" (default markdown) or "json" (normalized dict).
            fields: Override the YT `fields` selector. Empty uses a richer
                default with login on assignee, `presentation`/`text` on
                custom-field values, and tags + created.
            instance: YouTrack instance (optional).
        """
        client = resolver.resolve(instance, issue_id)
        issue_id = parse_issue_id(issue_id)

        if fields:
            field_set = fields
        else:
            # Default selector — richer than the historical set. Adds login
            # to assignee/comment-author (live YT filter URLs) and
            # presentation/text to custom-field values (deadlines, free-text).
            field_set = (
                "idReadable,summary,description,state(name),priority(name),"
                "assignee(login,name),created,updated,resolved,"
                "tags(name),customFields(name,value(name,login,presentation,text)),"
                "links(direction,linkType(name),issues(idReadable,summary,state(name),"
                "customFields(name,value(name))))"
            )
            if include_comments:
                field_set += ",comments(id,text,author(login,name),created)"

        data = await client.get(
            f"/api/issues/{issue_id}",
            params={"fields": field_set},
        )

        if format == "json":
            # Power override: caller asked for specific fields — return raw
            # so they get exactly what they requested. Otherwise normalize
            # to the cross-tool JSON shape.
            payload = data if fields else normalize_issue(data, include_comments=include_comments)
            return json.dumps(payload, indent=2, ensure_ascii=False)
        return format_issue_detail(data)

    @mcp.tool()
    async def get_issues(
        ids: str,
        fields: str = "",
        format: str = "report",
        include_comments: bool = False,
        instance: str = "",
    ) -> str:
        """Batch-fetch multiple issues in one round-trip.

        Composes a single `#A or #B or #C` query so a 20-issue enrichment
        pass takes one HTTP request instead of twenty. Useful when you
        already have a known ID list (active-focus picks, stuck-handoff
        IDs, daily-summary references).

        Reuses `get_issue`'s default field set + `normalize_issue` for
        consistent JSON shape across tools. Default `include_comments=False`
        because batch mode usually doesn't need them and they're heavy.

        Args:
            ids: Comma-separated issue IDs or URLs (PROJ-1, PROJ-2, ...).
                Up to ~100 IDs per call — large lists should be split
                across multiple calls.
            fields: Override the YT field selector. Empty uses
                `get_issue`'s expanded default.
            format: "report" (default compact list) or "json" (array of
                normalized issue dicts).
            include_comments: Include comments in the response
                (default False — batch mode rarely needs them).
            instance: YouTrack instance (optional).
        """
        client = resolver.resolve(instance)
        # Parse IDs, stripping URLs and whitespace.
        id_list = [parse_issue_id(s.strip()) for s in ids.split(",") if s.strip()]
        if not id_list:
            return "No issue IDs provided."

        # URL length scales with both ID count and selector size. ~100 IDs is
        # a safe upper bound for a typical YT deployment; warn rather than
        # silently truncate.
        if len(id_list) > 100:
            return (
                f"Too many IDs ({len(id_list)}): YT query URL length limits "
                "batch fetches to ~100 per call. Split into smaller batches."
            )

        query = " or ".join(f"#{iid}" for iid in id_list)

        if fields:
            field_set = fields
        else:
            field_set = (
                "idReadable,summary,description,state(name),priority(name),"
                "assignee(login,name),created,updated,resolved,"
                "tags(name),customFields(name,value(name,login,presentation,text)),"
                "links(direction,linkType(name),issues(idReadable,summary,state(name),"
                "customFields(name,value(name))))"
            )
            if include_comments:
                field_set += ",comments(id,text,author(login,name),created)"

        data = await client.get(
            "/api/issues",
            params={"query": query, "fields": field_set, "$top": str(len(id_list))},
        )
        data = data or []

        if format == "json":
            if fields:
                # Power override: return raw response array, no normalization.
                return json.dumps(data, indent=2, ensure_ascii=False)
            normalized = [normalize_issue(i, include_comments=include_comments) for i in data]
            return json.dumps(normalized, indent=2, ensure_ascii=False)

        # report (compact): one line per issue + a count header.
        lines = [f"## {len(data)} of {len(id_list)} issues fetched"]
        if len(data) < len(id_list):
            returned_ids = {i.get("idReadable", "") for i in data}
            missing = [i for i in id_list if i not in returned_ids]
            lines.append(f"_Missing (not found or access denied): {', '.join(missing)}_")
        lines.append("")
        lines.append(format_issue_list(data))
        return compact_lines(lines)

    @mcp.tool()
    async def create_issue(
        project: str, summary: str, description: str = "", product: str = "",
        command: str = "",
        instance: str = "",
    ) -> str:
        """Create a new issue in a YouTrack project.

        Use `command` to set required custom fields at creation time.

        Args:
            project: Project short name
            summary: Issue title
            description: Issue description (markdown)
            product: Product custom field (optional)
            command: YouTrack command for custom fields (e.g. 'Type Task Subsystem {Client Panel}')
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance)
        project_id = await client.resolve_project_id(project)
        if not project_id:
            return f"Project '{project}' not found."

        json_body: dict = {
            "project": {"id": project_id},
            "summary": summary,
            "description": description,
        }

        # Product is always a separate command (multi-word values)
        # User's command is tried as-is first (handles emoji/multi-word fields),
        # then split into individual pairs as fallback
        product_cmd = f"Product {product}" if product else ""
        split_commands: list[str] = []
        if command:
            for m in _CMD_FIELD_RE.finditer(command):
                name = m.group(1) or m.group(3)
                value = m.group(2) or m.group(4)
                # Emit BARE `name value` — never re-wrap in braces. Verified
                # live: YouTrack's *command* parser rejects braces in values
                # (`Status {New Employee}` -> 400 "expected: {New Employee}",
                # even single-word `Priority {High}`) and matches multi-word
                # enum values greedily, so `Status New Employee` is the correct
                # form. Braces are only an INPUT convention so this regex can
                # capture a multi-word value as one group (group 2 already
                # excludes the braces); they must not survive into the command.
                # (Re-wrapping here was the ADR-016/v1.16.6 regression.)
                split_commands.append(f"{name} {value}")

        failed_commands: list[str] = []
        # Lazily-computed field-aware split (fetched once, shared by the
        # direct and draft paths). The regex split above is the fallback when
        # the field list is unavailable or matches nothing.
        _split_cache: list[list[str]] = []

        async def _split_clauses() -> list[str]:
            if not _split_cache:
                names = await _get_project_field_names(client, project_id)
                clauses = _split_command_with_field_names(command, names) if names else []
                _split_cache.append(clauses or split_commands)
            return _split_cache[0]

        async def _apply_commands(target_id: str, *, use_internal_id: bool = False):
            """Apply product, then try user command as-is, split on failure."""
            issue_ref = {"id": target_id} if use_internal_id else {"idReadable": target_id}
            # Product always separate
            if product_cmd:
                try:
                    await client.post(
                        "/api/commands",
                        json={"query": product_cmd, "issues": [issue_ref]},
                    )
                except (httpx.HTTPStatusError, ValueError) as e:
                    failed_commands.append(f"`{product_cmd}`: {_cmd_error_text(e)}")
            if not command:
                return
            # Try the whole command first, BARE — braces are an input-only
            # convention (so _CMD_FIELD_RE can capture multi-word values); YT's
            # command parser rejects them, so strip before sending. This sets
            # simple/single-field commands in one call; a multi-field command
            # that doesn't parse as a whole falls through to the split below.
            whole = command.replace("{", "").replace("}", "")
            try:
                await client.post(
                    "/api/commands",
                    json={"query": whole, "issues": [issue_ref]},
                )
                return  # full command worked
            except (httpx.HTTPStatusError, ValueError):
                pass  # fall through to split (400 parse *and* 401/403 perm)
            # Split fallback: apply each field separately. Field-aware split
            # (project field names as boundaries) so multi-word/emoji field
            # NAMES ("Evaluation time 🕙") survive; regex split as fallback.
            split_failed: list[str] = []
            for cmd in await _split_clauses():
                try:
                    await client.post(
                        "/api/commands",
                        json={"query": cmd, "issues": [issue_ref]},
                    )
                except (httpx.HTTPStatusError, ValueError):
                    split_failed.append(cmd)
            # Rejoin failed splits and retry as single command
            # (handles multi-word/emoji fields like "Evaluation time 🕙 1h")
            if split_failed:
                rejoined = " ".join(split_failed)
                try:
                    await client.post(
                        "/api/commands",
                        json={"query": rejoined, "issues": [issue_ref]},
                    )
                except (httpx.HTTPStatusError, ValueError) as cmd_err:
                    failed_commands.append(f"`{rejoined}`: {_cmd_error_text(cmd_err)}")

        try:
            data = await client.post("/api/issues", json=json_body)
            issue_id = data.get("idReadable", "?")
            await _apply_commands(issue_id)
        except httpx.HTTPStatusError as perm_err:
            # 401/403 on the create itself = no Create Issue permission. Return
            # a clean, actionable message instead of a raw httpx error (which
            # leaks the instance URL) and — crucially — do NOT fall into the
            # draft path, which would leave an orphaned draft behind.
            code = perm_err.response.status_code
            if code in (401, 403):
                return (
                    f"**Could not create issue:** insufficient permissions to "
                    f"create issues in project **{project}** (HTTP {code}). "
                    f"Ask a YouTrack admin to grant the Create Issue permission."
                )
            raise
        except ValueError as e:
            if "required" not in str(e).lower() or not (command or product_cmd):
                raise
            # Required field missing — create as draft, apply commands, publish
            draft = await client.post(
                "/api/users/me/drafts", json=json_body,
            )
            draft_id = draft.get("id", "")
            if not draft_id:
                raise
            await _apply_commands(draft_id, use_internal_id=True)
            # Publish draft as a real issue (empty body — use draft's data)
            try:
                data = await client.post(
                    f"/api/issues?draftId={draft_id}&fields=idReadable,summary",
                    json={},
                )
                issue_id = data.get("idReadable", "?")
            except (httpx.HTTPStatusError, ValueError) as pub_err:
                # Publish failed — fetch required fields to help the LLM
                req_info = await _get_required_fields_info(client, project_id, project)
                return (
                    f"**Could not create issue:** {_cmd_error_text(pub_err)}\n\n"
                    + (f"**Failed commands:** {'; '.join(failed_commands)}\n" if failed_commands else "")
                    + (f"\n{req_info}" if req_info else "")
                    + "\nCreate the issue manually or adjust the command."
                )

        parts = [f"Created: **{issue_id}** — {data.get('summary', '')}"]
        if product:
            parts.append(f"**Product:** {product}")
        if command:
            parts.append(f"**Fields:** {command}")
        if failed_commands:
            parts.append(f"\n**Could not set:** {'; '.join(failed_commands)}")
            parts.append("Set these fields manually in YouTrack.")
        return " | ".join(parts[:3]) + ("".join(parts[3:]) if len(parts) > 3 else "")

    @mcp.tool()
    async def update_issue(
        issue_id: str,
        summary: str = "",
        description: str = "",
        state: str = "",
        assignee: str = "",
        product: str = "",
        add_tag: str = "",
        remove_tag: str = "",
        command: str = "",
        instance: str = "",
    ) -> str:
        """Update fields of a YouTrack issue. Returns previous values for rollback.

        Args:
            issue_id: Issue ID or URL
            summary: New title (empty = keep)
            description: New description (empty = keep)
            state: New state name (empty = keep)
            assignee: New assignee (empty = keep)
            product: Product field (empty = keep)
            add_tag: Tag to add (empty = skip)
            remove_tag: Tag to remove (empty = skip)
            command: YouTrack command for any field (e.g. 'Priority High Type Bug')
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance, issue_id)
        issue_id = parse_issue_id(issue_id)
        has_changes = (
            summary or description or state or assignee
            or product or add_tag or remove_tag or command
        )
        if not has_changes:
            return "Nothing to update — provide at least one field or command."

        # Snapshot before changes for rollback
        before = await client.get(
            f"/api/issues/{issue_id}",
            params={
                "fields": "idReadable,summary,description,"
                "state(name),assignee(name),tags(name),"
                "customFields(name,value(name,login))",
            },
        )

        old_summary = before.get("summary", "?")
        old_state = _resolve_state(before)
        old_assignee = _resolve_assignee(before)
        old_tags = [t.get("name", "") for t in before.get("tags", [])]

        # Collect old custom field values for rollback info
        old_fields: dict[str, str] = {}
        for cf in before.get("customFields", []):
            cf_name = cf.get("name", "")
            cf_value = cf.get("value")
            if cf_value is None:
                old_fields[cf_name] = "(empty)"
            elif isinstance(cf_value, list):
                old_fields[cf_name] = ", ".join(
                    v.get("name", v.get("login", "?")) for v in cf_value
                )
            elif isinstance(cf_value, dict):
                old_fields[cf_name] = cf_value.get("name", cf_value.get("login", "?"))
            else:
                old_fields[cf_name] = str(cf_value)

        # Apply REST API changes (summary, description)
        payload: dict = {}
        if summary:
            payload["summary"] = summary
        if description:
            payload["description"] = description

        if payload:
            await client.post(f"/api/issues/{issue_id}", json=payload)

        # Build command string from explicit params + raw command
        commands = []
        if state:
            commands.append(f"State {state}")
        if assignee:
            commands.append(f"Assignee {assignee}")
        if product:
            commands.append(f"Product {product}")
        if add_tag:
            commands.append(f"tag {add_tag}")
        if remove_tag:
            commands.append(f"untag {remove_tag}")
        if command:
            commands.append(command)

        if commands:
            await client.execute_command(issue_id, " ".join(commands))

        # Fetch updated state
        after = await client.get(
            f"/api/issues/{issue_id}",
            params={
                "fields": "idReadable,summary,state(name),assignee(name),"
                "customFields(name,value(name,login)),tags(name)",
            },
        )
        new_state = _resolve_state(after)
        new_assignee = _resolve_assignee(after)
        new_tags = [t.get("name", "") for t in after.get("tags", [])]

        # Build response with changes + rollback info
        parts = [
            f"Updated: **{after.get('idReadable', '?')}** — {after.get('summary', '')}"
        ]

        # Show what changed
        changes = []
        if summary and summary != old_summary:
            changes.append(f"**Summary:** {old_summary} → {summary}")
        if new_state != old_state:
            changes.append(f"**State:** {old_state} → {new_state}")
        if new_assignee != old_assignee:
            changes.append(f"**Assignee:** {old_assignee} → {new_assignee}")
        if new_tags != old_tags:
            changes.append(f"**Tags:** {', '.join(old_tags) or '(none)'} → {', '.join(new_tags) or '(none)'}")

        # Check custom fields for changes
        for cf in after.get("customFields", []):
            cf_name = cf.get("name", "")
            cf_value = cf.get("value")
            if cf_value is None:
                new_val = "(empty)"
            elif isinstance(cf_value, list):
                new_val = ", ".join(
                    v.get("name", v.get("login", "?")) for v in cf_value
                )
            elif isinstance(cf_value, dict):
                new_val = cf_value.get("name", cf_value.get("login", "?"))
            else:
                new_val = str(cf_value)
            old_val = old_fields.get(cf_name, "(empty)")
            if new_val != old_val:
                changes.append(f"**{cf_name}:** {old_val} → {new_val}")

        if changes:
            parts.append("")
            parts.extend(changes)
        else:
            parts.append("No field changes detected.")

        # Rollback instructions
        rollback_parts = []
        if summary:
            rollback_parts.append(f"summary=\"{old_summary}\"")
        if state:
            rollback_parts.append(f"state=\"{old_state}\"")
        if assignee:
            rollback_parts.append(f"assignee=\"{old_assignee}\"")
        if command:
            rollback_parts.append(f"(use `rollback_issue` with activity ID for command fields)")

        if rollback_parts:
            parts.append("")
            parts.append(f"To restore: `update_issue({issue_id}, {', '.join(rollback_parts)})`")

        return compact_lines(parts)

    @mcp.tool()
    async def transition_issue(
        issue_id: str,
        state: str,
        set_fields: str = "",
        instance: str = "",
    ) -> str:
        """Change an issue's State, gate-aware: set required fields first, then
        transition, and report the exact workflow rule if it still blocks.

        YouTrack workflow scripts often gate transitions ("set Dev Estimation
        before To Do"). A raw update fails one opaque 400 at a time; this tool
        applies `set_fields` field-by-field first (command syntax, e.g.
        'Dev Estimation 2d QA Estimation 1d'), then attempts the state change.
        If a gate still blocks, the blocking rule's own text is returned so
        the caller knows exactly what to supply — nothing is ever invented.

        Args:
            issue_id: Issue ID or URL
            state: Target state name (bare, e.g. 'To Do', 'Ready for QA')
            set_fields: Fields to set before transitioning (command syntax; optional)
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance)
        issue_id = parse_issue_id(issue_id)
        try:
            before = await client.get(
                f"/api/issues/{issue_id}",
                params={
                    "fields": "idReadable,summary,project(id),"
                    "customFields(name,value(name))",
                },
            )
        except ValueError as e:
            return f"Could not read {issue_id}: {e}"
        # Projects name their state field differently ("State" on dev boards,
        # "Status" on e.g. HR) — command syntax needs the real field name.
        state_field = "State"
        for cf in before.get("customFields", []):
            if cf.get("name") in ("State", "Status"):
                state_field = cf["name"]
                break
        old_state = _get_custom_field(before, state_field) or _resolve_state(before)
        project_id = (before.get("project") or {}).get("id", "")
        # Internal ids ("87-61285", e.g. drafts) need {"id": ...};
        # readable ids ("PROJ-7") use {"idReadable": ...}.
        internal = issue_id.split("-", 1)[0].isdigit()
        issue_ref = {"id": issue_id} if internal else {"idReadable": issue_id}

        applied: list[str] = []
        failed: list[str] = []
        if set_fields:
            names = await _get_project_field_names(client, project_id) if project_id else []
            clauses = _split_command_with_field_names(set_fields, names) or [
                f"{(m.group(1) or m.group(3))} {(m.group(2) or m.group(4))}"
                for m in _CMD_FIELD_RE.finditer(set_fields)
            ]
            for clause in clauses:
                try:
                    await client.post(
                        "/api/commands",
                        json={"query": clause, "issues": [issue_ref]},
                    )
                    applied.append(clause)
                except (httpx.HTTPStatusError, ValueError) as e:
                    failed.append(f"`{clause}`: {_cmd_error_text(e)}")

        state_bare = state.replace("{", "").replace("}", "").strip()
        parts: list[str] = []
        try:
            await client.post(
                "/api/commands",
                json={"query": f"{state_field} {state_bare}", "issues": [issue_ref]},
            )
        except (httpx.HTTPStatusError, ValueError) as e:
            parts.append(
                f"⛔ **{issue_id}: transition to '{state_bare}' blocked** — "
                f"{_cmd_error_text(e)}"
            )
            parts.append(f"**Current state:** {old_state}")
            if applied:
                parts.append(f"**Fields set:** {'; '.join(applied)}")
            if failed:
                parts.append(f"**Fields NOT set:** {'; '.join(failed)}")
            parts.append(
                "If the message above names a required field, pass it via "
                "`set_fields` (e.g. `set_fields=\"Dev Estimation 2d QA Estimation 1d\"`) "
                "and retry."
            )
            return compact_lines(parts)

        # Verify the state actually changed (workflows can silently no-op).
        try:
            after = await client.get(
                f"/api/issues/{issue_id}",
                params={"fields": "customFields(name,value(name))"},
            )
            new_state = _get_custom_field(after, state_field) or _resolve_state(after)
        except ValueError:
            new_state = state_bare
        parts.append(f"✅ **{issue_id}**: State {old_state} → {new_state}")
        if applied:
            parts.append(f"**Fields set:** {'; '.join(applied)}")
        if failed:
            parts.append(f"**Fields NOT set:** {'; '.join(failed)}")
        if new_state.lower() != state_bare.lower():
            parts.append(
                f"⚠ Requested '{state_bare}' but the issue reads '{new_state}' — "
                "a workflow rule may have redirected the transition."
            )
        return compact_lines(parts)

    @mcp.tool()
    async def delete_issue(issue_id: str, permanent: bool = False, instance: str = "") -> str:
        """Delete a YouTrack issue. Default: soft delete (state Obsolete). permanent=True is irreversible.

        Args:
            issue_id: Issue ID or URL
            permanent: Permanently delete (default: False, soft delete)
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance, issue_id)
        issue_id = parse_issue_id(issue_id)
        data = await client.get(
            f"/api/issues/{issue_id}",
            params={
                "fields": "idReadable,summary,state(name),"
                "customFields(name,value(name))",
            },
        )
        summary = data.get("summary", "")
        old_state = _resolve_state(data)

        if permanent:
            await client.delete(f"/api/issues/{issue_id}")
            return f"Permanently deleted: **{issue_id}** — {summary}"

        await client.execute_command(issue_id, "State Obsolete")
        return (
            f"Soft-deleted: **{issue_id}** — {summary}\n"
            f"**State:** {old_state} → Obsolete"
        )

    @mcp.tool()
    async def get_issue_links(issue_id: str, instance: str = "") -> str:
        """Get all linked issues for an issue.

        Args:
            issue_id: Issue ID or URL
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance, issue_id)
        issue_id = parse_issue_id(issue_id)
        data = await client.get(
            f"/api/issues/{issue_id}",
            params={
                "fields": "idReadable,summary,"
                "links(direction,linkType(name),"
                "issues(idReadable,summary,state(name),"
                "customFields(name,value(name))))",
            },
        )

        links = data.get("links", [])
        if not links:
            return f"No links found for **{issue_id}**."

        # Group by link type + direction
        groups: dict[str, list[str]] = {}
        for link in links:
            link_type = (link.get("linkType") or {}).get("name", "?")
            direction = link.get("direction", "BOTH")
            # Build a label from type + direction
            if direction == "OUTWARD":
                label = link_type
            elif direction == "INWARD":
                label = f"{link_type} (inward)"
            else:
                label = link_type

            for linked in link.get("issues", []):
                ls = linked.get("state")
                if ls and isinstance(ls, dict) and ls.get("name"):
                    linked_state = ls["name"]
                else:
                    linked_state = _get_custom_field(linked, "State") or "?"
                line = (
                    f"- {linked.get('idReadable', '?')} [{linked_state}] "
                    f"{linked.get('summary', '')}"
                )
                groups.setdefault(label, []).append(line)

        parts = [f"## Links for {data.get('idReadable', issue_id)}"]
        for label, items in groups.items():
            parts.append(f"\n### {label}")
            parts.extend(items)

        return compact_lines(parts)

    @mcp.tool()
    async def add_issue_link(
        issue_id: str,
        target_id: str,
        link_type: str = "Relates",
        instance: str = "",
    ) -> str:
        """Link two issues together.

        Args:
            issue_id: Source issue ID or URL
            target_id: Target issue ID or URL
            link_type: Relation type (default: 'Relates')
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance, issue_id)
        issue_id = parse_issue_id(issue_id)
        target_id = parse_issue_id(target_id)
        command = f"{link_type} {target_id}"
        await client.execute_command(issue_id, command)
        return f"Linked **{issue_id}** → **{target_id}** ({link_type})"

    @mcp.tool()
    async def remove_issue_link(
        issue_id: str,
        target_id: str,
        link_type: str = "Relates",
        instance: str = "",
    ) -> str:
        """Remove a link between two issues.

        Args:
            issue_id: Source issue ID or URL
            target_id: Target issue ID or URL
            link_type: Relation type to remove (default: 'Relates')
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance, issue_id)
        issue_id = parse_issue_id(issue_id)
        target_id = parse_issue_id(target_id)
        command = f"remove {link_type} {target_id}"
        await client.execute_command(issue_id, command)
        return f"Unlinked **{issue_id}** → **{target_id}** ({link_type})"

    @mcp.tool()
    async def poll_changes(
        query: str = "",
        since_minutes: int = 5,
        max_results: int = 50,
        instance: str = "",
    ) -> str:
        """Poll for recently changed issues within the last N minutes.

        Args:
            query: YouTrack query filter (optional)
            since_minutes: Minutes to look back (default: 5)
            max_results: Max results (default: 50)
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance)

        since_ts = int((datetime.now(tz=timezone.utc).timestamp() - since_minutes * 60) * 1000)

        # Fetch issues and filter by updated timestamp client-side
        # (avoids YouTrack query syntax compatibility issues)
        full_query = query if query else "#Unresolved"

        data = await client.get(
            "/api/issues",
            params={
                "query": full_query,
                "fields": "idReadable,summary,state(name),assignee(name),"
                "customFields(name,value(name)),updated",
                "$top": "200",
            },
        )

        # Filter to issues updated since the cutoff
        data = [i for i in data if i.get("updated", 0) >= since_ts]
        data = data[:max_results]

        if not data:
            return f"No changes in the last {since_minutes} minutes."

        lines = [
            f"## Changes in the last {since_minutes} minutes",
            f"**Query:** `{full_query}`",
            f"**Issues changed:** {len(data)}",
            "",
        ]

        for issue in data:
            issue_id = issue.get("idReadable", "?")
            summary = issue.get("summary", "?")
            state = _resolve_state(issue)
            assignee = _resolve_assignee(issue)
            updated_ms = issue.get("updated")
            updated_str = ""
            if updated_ms:
                updated_str = datetime.fromtimestamp(
                    updated_ms / 1000, tz=timezone.utc
                ).strftime("%H:%M UTC")

            lines.append(
                f"- **{issue_id}** [{state}] {summary} → {assignee} ({updated_str})"
            )

        if len(data) >= max_results:
            lines.append(f"\n*Showing first {max_results}, more may exist.*")

        return compact_lines(lines)

    @mcp.tool()
    async def count_issues(query: str, instance: str = "") -> str:
        """Count issues matching a YouTrack query.

        Args:
            query: YouTrack search query
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance)
        data = await client.get(
            "/api/issues",
            params={
                "query": query,
                "fields": "idReadable",
                "$top": "500",
            },
        )
        count = len(data)
        suffix = "+" if count >= 500 else ""
        return f"**{count}{suffix}** issues match query: `{query}`"
