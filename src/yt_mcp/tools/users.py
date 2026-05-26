import json

from yt_mcp.resolver import InstanceResolver


def register(mcp, resolver: InstanceResolver):

    @mcp.tool()
    async def get_current_user(format: str = "report", instance: str = "") -> str:
        """Get the currently authenticated YouTrack user.

        `format="json"` returns a structured dict including `instance_url`
        for downstream consumers building issue hyperlinks (e.g. email
        reports). `format="report"` (default) preserves the chat-friendly
        markdown view.

        Args:
            format: "report" (default, markdown) or "json" (structured).
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance)
        data = await client.get(
            "/api/users/me",
            params={"fields": "id,login,fullName,email,online,banned,avatarUrl"},
        )

        if format == "json":
            return json.dumps({
                "id": data.get("id"),
                "login": data.get("login"),
                "name": data.get("fullName"),
                "email": data.get("email"),
                "online": bool(data.get("online")),
                "banned": bool(data.get("banned")),
                "avatar_url": data.get("avatarUrl"),
                "instance_url": client.base_url,
            }, indent=2, ensure_ascii=False)

        status = ""
        if data.get("banned"):
            status = " (BANNED)"
        elif data.get("online"):
            status = " (online)"

        parts = [
            f"## Current user",
            f"**Name:** {data.get('fullName', '?')}",
            f"**Login:** {data.get('login', '?')}",
        ]
        if data.get("email"):
            parts.append(f"**Email:** {data['email']}")
        parts.append(f"**ID:** {data.get('id', '?')}{status}")
        parts.append(f"**Instance:** {client.base_url}")
        return "\n".join(parts)

    @mcp.tool()
    async def get_instance_url(format: str = "report", instance: str = "") -> str:
        """Return the base URL of the configured YouTrack instance.

        Cheap, no-auth-state probe — useful for downstream renderers (email
        templates, dashboards) that need `<base>/issue/<ID>` links without
        making a who-am-I call.

        Args:
            format: "report" (default plain text) or "json" ({"base_url": "..."}).
            instance: YouTrack instance (optional).
        """
        client = resolver.resolve(instance)
        if format == "json":
            return json.dumps({"base_url": client.base_url}, ensure_ascii=False)
        return client.base_url

    @mcp.tool()
    async def search_users(query: str, max_results: int = 20, instance: str = "") -> str:
        """Search YouTrack users by name or login.

        Args:
            query: Search string
            max_results: Max results (default: 20)
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance)
        users = await client.get(
            "/api/users",
            params={
                "query": query,
                "fields": "id,login,fullName,email,online,banned",
                "$top": str(max_results),
            },
        )

        if not users:
            return f"No users found matching '{query}'."

        lines = [f"**Found: {len(users)} users**", ""]
        for u in users:
            status = ""
            if u.get("banned"):
                status = " [BANNED]"
            name = u.get("fullName", "?")
            login = u.get("login", "?")
            email = u.get("email", "")
            email_str = f" ({email})" if email else ""
            lines.append(f"- **{name}** @{login}{email_str}{status}")
        return "\n".join(lines)
