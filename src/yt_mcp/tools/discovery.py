from yt_mcp.resolver import InstanceResolver
from yt_mcp.formatters import format_issue_list


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
