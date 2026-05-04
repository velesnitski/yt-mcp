import asyncio

from yt_mcp.resolver import InstanceResolver
from yt_mcp.formatters import (
    format_issue_list, parse_issue_id, compact_lines,
    _resolve_state, _resolve_assignee, _get_custom_field, escape_query_value,
)


def register(mcp, resolver: InstanceResolver):

    @mcp.tool()
    async def list_tags(instance: str = "") -> str:
        """List all issue tags with issue counts.

        Args:
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance)
        tags = await client.get(
            "/api/issueTags",
            params={"fields": "name,issues(id)", "$top": "100"},
        )
        if not tags:
            return "No tags found."

        lines = []
        for t in tags:
            name = t.get("name", "?")
            count = len(t.get("issues", []))
            lines.append(f"- **{name}** ({count} issues)")
        return "\n".join(lines)

    @mcp.tool()
    async def list_saved_searches(instance: str = "") -> str:
        """List all saved searches (queries).

        Args:
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance)
        queries = await client.get(
            "/api/savedQueries",
            params={"fields": "name,query"},
        )
        if not queries:
            return "No saved searches found."

        lines = []
        for q in queries:
            lines.append(f"- **{q.get('name', '?')}**: `{q.get('query', '?')}`")
        return "\n".join(lines)

    @mcp.tool()
    async def run_saved_search(name: str, max_results: int = 50, instance: str = "") -> str:
        """Run a saved search by name and return matching issues.

        Args:
            name: Saved search name (partial match)
            max_results: Max results (default: 50)
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance)
        queries = await client.get(
            "/api/savedQueries",
            params={"fields": "name,query"},
        )

        name_lower = name.lower()
        matches = [q for q in queries if name_lower in q.get("name", "").lower()]
        if not matches:
            return f"No saved search found matching '{name}'."
        if len(matches) > 1:
            names = ", ".join(f"'{q.get('name', '?')}'" for q in matches)
            return f"Multiple saved searches match '{name}': {names}. Be more specific."

        query = matches[0].get("query", "")
        query_name = matches[0].get("name", name)

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
        header = f"**Saved search:** {query_name}\n**Query:** `{query}`\n**Found: {count} issues**"
        if count >= max_results:
            header += f" (showing first {max_results}, more may exist)"
        if count == 0:
            return f"{header}\n\nNo issues match this saved search."
        return f"{header}\n\n{result}"

    @mcp.tool()
    async def audit_issue_list(issue_ids: str, instance: str = "") -> str:
        """Get current status, assignee, and last update for a list of issues.

        Useful for validating a list (e.g. roadmap, priorities) against current state.

        Args:
            issue_ids: Comma-separated issue IDs or URLs (e.g. 'PROJ-1,PROJ-2,PROJ-3')
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance)
        ids = [parse_issue_id(i.strip()) for i in issue_ids.split(",") if i.strip()]
        if not ids:
            return "No issue IDs provided."

        fields = (
            "idReadable,summary,state(name),assignee(name),updated,resolved,"
            "customFields(name,value(name))"
        )

        # Single batch query: `issue ID: A-1, B-2, C-3` is OR-joined in YT
        batch_query = "issue ID: " + ", ".join(ids)
        try:
            data_list = await client.get(
                "/api/issues",
                params={"query": batch_query, "fields": fields, "$top": str(max(len(ids), 100))},
            )
        except ValueError:
            data_list = []

        by_id = {issue.get("idReadable", ""): issue for issue in data_list if issue.get("idReadable")}

        from datetime import datetime, timezone
        now = datetime.now(tz=timezone.utc)

        lines = [f"## Audit of {len(ids)} issues"]
        not_found: list[str] = []
        resolved_ids: list[str] = []

        for iid in ids:
            data = by_id.get(iid)
            if not data:
                not_found.append(iid)
                continue
            state = _resolve_state(data)
            assignee = _resolve_assignee(data)
            updated_ms = data.get("updated", 0)
            resolved_ms = data.get("resolved")
            days_idle = (
                (now.timestamp() * 1000 - updated_ms) / 86400000
                if updated_ms else 0
            )
            idle_str = f"{int(days_idle)}d idle" if days_idle else "?"
            status_marker = " ✓ resolved" if resolved_ms else ""
            if resolved_ms:
                resolved_ids.append(iid)
            lines.append(
                f"- **{data.get('idReadable', iid)}** [{state}]{status_marker} "
                f"→ {assignee} | {idle_str} | {data.get('summary', '?')}"
            )

        if not_found:
            lines.append("")
            lines.append(f"**Not found ({len(not_found)}):** {', '.join(not_found)}")
        if resolved_ids:
            lines.append("")
            lines.append(
                f"**Resolved ({len(resolved_ids)}):** "
                f"{', '.join(resolved_ids)} — may need to remove from list"
            )
        return compact_lines(lines)

    @mcp.tool()
    async def compare_issue_lists(known_ids: str, query: str, instance: str = "") -> str:
        """Diff a known issue list against a YouTrack query.

        Returns: matched (in both), missing-from-list (in query, not your list),
        no-longer-matching (in your list, not in query). Useful for finding tasks
        you're not tracking, or items in your list that are stale.

        Args:
            known_ids: Comma-separated issue IDs you already know (e.g. 'PROJ-1,PROJ-2')
            query: YouTrack query to compare against (e.g. 'project: MAN #Unresolved')
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance)
        known = {parse_issue_id(i.strip()) for i in known_ids.split(",") if i.strip()}
        if not known:
            return "No known IDs provided."

        data = await client.get(
            "/api/issues",
            params={
                "query": query,
                "fields": "idReadable,summary,state(name),assignee(name)",
                "$top": "500",
            },
        )

        actual = {issue.get("idReadable", ""): issue for issue in data if issue.get("idReadable")}
        actual_ids = set(actual.keys())

        matched = known & actual_ids
        missing_from_list = actual_ids - known  # in query, not in known
        no_longer_matching = known - actual_ids  # in known, not in query

        lines = [
            f"## List comparison",
            f"**Query:** `{query}`",
            f"**Your list:** {len(known)} issues | **Query result:** {len(actual_ids)}",
            "",
            f"**Matched ({len(matched)}):** {', '.join(sorted(matched)) if matched else 'none'}",
            "",
        ]

        if missing_from_list:
            lines.append(f"**🔴 Missing from your list ({len(missing_from_list)})** — match query but not tracked:")
            for iid in sorted(missing_from_list):
                issue = actual[iid]
                state = _resolve_state(issue)
                assignee = _resolve_assignee(issue)
                lines.append(f"- **{iid}** [{state}] → {assignee} | {issue.get('summary', '?')}")
            lines.append("")

        if no_longer_matching:
            lines.append(
                f"**⚠️ No longer matching ({len(no_longer_matching)})** — in your list but not in query "
                f"(may be resolved, moved, or closed):"
            )
            lines.append(f"- {', '.join(sorted(no_longer_matching))}")

        if not missing_from_list and not no_longer_matching:
            lines.append("**✓ Your list is in sync with the query.**")

        return compact_lines(lines)

    @mcp.tool()
    async def get_roadmap(
        projects: str = "",
        types: str = "",
        states: str = "",
        max_per_project: int = 50,
        instance: str = "",
    ) -> str:
        """Cross-project roadmap view filtered by type and state.

        Args:
            projects: Comma-separated project shortnames (empty = all accessible)
            types: Comma-separated Type values (e.g. 'Epic,Product Task') (empty = all)
            states: Comma-separated states (empty = all unresolved)
            max_per_project: Max issues per project (default: 50)
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance)

        if projects:
            project_list = [p.strip() for p in projects.split(",") if p.strip()]
        else:
            all_projects = await client.get(
                "/api/projects", params={"fields": "shortName", "$top": "100"}
            )
            project_list = [p.get("shortName", "") for p in all_projects if p.get("shortName")]

        type_list = [t.strip() for t in types.split(",") if t.strip()]
        state_list = [s.strip() for s in states.split(",") if s.strip()]

        # Build query parts
        type_filter = ""
        if type_list:
            type_filter = " " + " ".join(
                f"Type: {{{escape_query_value(t)}}}" if " " in t else f"Type: {escape_query_value(t)}"
                for t in type_list
            )

        state_filter = ""
        if state_list:
            state_filter = " " + " ".join(
                f"State: {{{escape_query_value(s)}}}" if " " in s else f"State: {escape_query_value(s)}"
                for s in state_list
            )
        else:
            state_filter = " #Unresolved"

        async def _fetch_project(proj: str) -> tuple[str, list]:
            query = f"project: {escape_query_value(proj)}{type_filter}{state_filter}"
            try:
                data = await client.get(
                    "/api/issues",
                    params={
                        "query": query,
                        "fields": "idReadable,summary,state(name),assignee(name),"
                        "customFields(name,value(name))",
                        "$top": str(max_per_project),
                    },
                )
                return proj, data
            except ValueError:
                return proj, []

        results = await asyncio.gather(*(_fetch_project(p) for p in project_list))

        total = sum(len(issues) for _, issues in results)
        lines = [
            f"# Roadmap — {len(project_list)} projects",
            f"**Filters:** types={types or 'any'}, states={states or 'unresolved'}",
            f"**Total issues:** {total}",
            "",
        ]

        for proj, issues in sorted(results, key=lambda x: -len(x[1])):
            if not issues:
                continue
            lines.append(f"## {proj} ({len(issues)})")
            for issue in issues:
                iid = issue.get("idReadable", "?")
                state = _resolve_state(issue)
                assignee = _resolve_assignee(issue)
                product = _get_custom_field(issue, "Product") or ""
                product_str = f" [{product}]" if product else ""
                lines.append(
                    f"- **{iid}** [{state}]{product_str} → {assignee} | {issue.get('summary', '?')}"
                )
            lines.append("")

        return compact_lines(lines)
