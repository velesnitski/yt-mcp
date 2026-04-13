from datetime import datetime, timezone

from yt_mcp.resolver import InstanceResolver
from yt_mcp.formatters import parse_issue_id, compact_lines


def register(mcp, resolver: InstanceResolver):

    @mcp.tool()
    async def list_attachments(issue_id: str, instance: str = "") -> str:
        """List all attachments on a YouTrack issue.

        Args:
            issue_id: Issue ID or URL
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance, issue_id)
        issue_id = parse_issue_id(issue_id)
        data = await client.get(
            f"/api/issues/{issue_id}",
            params={
                "fields": "idReadable,attachments(id,name,size,mimeType,url,author(name),created)",
            },
        )

        attachments = data.get("attachments", [])
        if not attachments:
            return f"No attachments on **{data.get('idReadable', issue_id)}**."

        lines = [f"## Attachments for {data.get('idReadable', issue_id)} ({len(attachments)})"]
        for a in attachments:
            size_kb = (a.get("size", 0) or 0) / 1024
            author = a.get("author", {})
            author_name = author.get("name", "?") if author else "?"
            created = a.get("created")
            date_str = ""
            if created:
                try:
                    date_str = datetime.fromtimestamp(
                        created / 1000, tz=timezone.utc
                    ).strftime("%Y-%m-%d %H:%M")
                except (OSError, ValueError):
                    pass
            lines.append(
                f"- **{a.get('name', '?')}** ({size_kb:.1f} KB, {a.get('mimeType', '?')}) "
                f"by {author_name} {date_str}"
            )
        return compact_lines(lines)

    @mcp.tool()
    async def get_attachment_url(issue_id: str, attachment_name: str, instance: str = "") -> str:
        """Get the download URL for a specific attachment on an issue.

        Args:
            issue_id: Issue ID or URL
            attachment_name: Attachment file name (partial match)
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance, issue_id)
        issue_id = parse_issue_id(issue_id)
        data = await client.get(
            f"/api/issues/{issue_id}",
            params={
                "fields": "idReadable,attachments(id,name,url)",
            },
        )

        attachments = data.get("attachments", [])
        if not attachments:
            return f"No attachments on **{data.get('idReadable', issue_id)}**."

        name_lower = attachment_name.lower()
        matches = [a for a in attachments if name_lower in a.get("name", "").lower()]

        if not matches:
            names = ", ".join(f"'{a.get('name', '?')}'" for a in attachments)
            return f"No attachment matching '{attachment_name}'. Available: {names}"
        if len(matches) > 1:
            names = ", ".join(f"'{a.get('name', '?')}'" for a in matches)
            return f"Multiple attachments match '{attachment_name}': {names}. Be more specific."

        att = matches[0]
        url = att.get("url", "")
        if url and not url.startswith("http"):
            base = client._config.url.rstrip("/")
            url = f"{base}{url}"

        return f"**{att.get('name', '?')}** on {data.get('idReadable', issue_id)}\n**URL:** {url}"
