import base64
import binascii
import mimetypes
import os
from datetime import datetime, timezone

from yt_mcp.resolver import InstanceResolver
from yt_mcp.formatters import parse_issue_id, compact_lines


def _guess_mime(name: str, *, default: str) -> str:
    """Best-effort MIME from filename extension, with a mode-appropriate default."""
    guessed, _ = mimetypes.guess_type(name)
    return guessed or default


def _full_url(client, url: str) -> str:
    """Absolute URL for a (possibly relative) YouTrack attachment path."""
    if url and not url.startswith("http"):
        return f"{client.base_url.rstrip('/')}{url}"
    return url


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
        url = _full_url(client, att.get("url", ""))

        return f"**{att.get('name', '?')}** on {data.get('idReadable', issue_id)}\n**URL:** {url}"

    @mcp.tool()
    async def add_attachment(
        issue_id: str,
        file_path: str = "",
        content: str = "",
        filename: str = "",
        content_base64: bool = False,
        mime_type: str = "",
        instance: str = "",
    ) -> str:
        """Attach a file to a YouTrack issue.

        Two input modes:
          - `file_path`: upload an existing file from disk (report.html,
            chart.png, data.xlsx). `filename` overrides the displayed name.
          - `content` + `filename`: upload generated/inline content with no
            temp file. Text is UTF-8 encoded; set `content_base64=True` to
            upload binary you already hold as base64 (e.g. a screenshot).

        For plain-text or markdown reports, prefer `add_comment` — it renders
        inline, is searchable, and notifies watchers. Reach for this when the
        artifact is binary (HTML/Excel/PDF/image) or a downloadable file is
        genuinely wanted.

        Args:
            issue_id: Issue ID or URL.
            file_path: Path to a local file to upload (mode A).
            content: Inline content to upload (mode B; requires `filename`).
            filename: Display name. Required for `content`; optional override
                for `file_path` (defaults to the file's basename).
            content_base64: Treat `content` as base64-encoded binary.
            mime_type: Override the auto-detected MIME type.
            instance: YouTrack instance (optional).
        """
        client = resolver.resolve(instance, issue_id)
        issue_id = parse_issue_id(issue_id)

        if not file_path and not content:
            return "Provide either `file_path` (a local file) or `content` (inline data + `filename`)."
        if file_path and content:
            return "Provide only one of `file_path` or `content`, not both."

        if file_path:
            if not os.path.isfile(file_path):
                return f"File not found: `{file_path}`"
            try:
                with open(file_path, "rb") as f:
                    data_bytes = f.read()
            except OSError as e:
                return f"Could not read `{file_path}`: {e}"
            name = filename or os.path.basename(file_path)
            mime = mime_type or _guess_mime(name, default="application/octet-stream")
        else:
            if not filename:
                return "`filename` is required when uploading `content`."
            name = filename
            if content_base64:
                try:
                    data_bytes = base64.b64decode(content, validate=True)
                except (binascii.Error, ValueError):
                    return "`content` is not valid base64 (set content_base64=False for plain text)."
                mime = mime_type or _guess_mime(name, default="application/octet-stream")
            else:
                data_bytes = content.encode("utf-8")
                mime = mime_type or _guess_mime(name, default="text/plain")

        if not data_bytes:
            return f"Refusing to upload an empty file (`{name}`)."

        result = await client.post_multipart(
            f"/api/issues/{issue_id}/attachments",
            files={"file": (name, data_bytes, mime)},
            params={"fields": "id,name,size,url"},
        )
        # YouTrack returns either the created attachment or a list of them.
        att = result[0] if isinstance(result, list) and result else result
        if not isinstance(att, dict):
            att = {}

        size_kb = (att.get("size", len(data_bytes)) or 0) / 1024
        url = _full_url(client, att.get("url", ""))
        lines = [
            f"✓ Attached **{att.get('name', name)}** to {issue_id} "
            f"({size_kb:.1f} KB, {mime})",
        ]
        if url:
            lines.append(f"**URL:** {url}")
        return compact_lines(lines)
