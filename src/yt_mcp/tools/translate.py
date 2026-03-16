import re
import time

import httpx

from yt_mcp.resolver import InstanceResolver


def _has_cyrillic(text: str) -> bool:
    """Check if text contains Cyrillic characters."""
    return bool(re.search(r"[\u0400-\u04FF]", text))


def register(mcp, resolver: InstanceResolver):

    @mcp.tool()
    async def get_issues_for_translation(
        query: str,
        include_comments: bool = True,
        max_results: int = 10,
        instance: str = "",
    ) -> str:
        """Fetch issues that need translation, returning their raw text content.

        Returns issue IDs, summaries, descriptions, and comments as structured text.
        The LLM should translate the text and then call apply_translations to write it back.

        Only includes issues where summary contains Cyrillic characters (likely Russian).

        Args:
            query: YouTrack search query (e.g., 'project: AP state: Open')
            include_comments: Whether to include comments for translation (default: True)
            max_results: Batch size (default: 10, keep small to fit in context)
            instance: YouTrack instance name (optional, for multi-instance setups)
        """
        client = resolver.resolve(instance)
        comment_fields = ",comments(id,text,author(name))" if include_comments else ""
        issues = await client.get(
            "/api/issues",
            params={
                "query": query,
                "fields": f"idReadable,summary,description{comment_fields}",
                "$top": str(max_results),
            },
        )

        if not issues:
            return f"No issues match query: `{query}`"

        # Filter to issues with Cyrillic text
        to_translate = []
        skipped = 0
        for issue in issues:
            summary = issue.get("summary", "")
            desc = issue.get("description", "") or ""
            if _has_cyrillic(summary) or _has_cyrillic(desc):
                to_translate.append(issue)
            else:
                skipped += 1

        if not to_translate:
            return f"No issues need translation (all {len(issues)} are already in target language)."

        # Build structured output for the LLM to translate
        lines = [
            "## Issues for translation",
            f"**Query:** `{query}`",
            f"**Issues to translate:** {len(to_translate)} (skipped {skipped} already in English)",
            "",
            "Translate the text below and call `apply_translations` with the results.",
            "Preserve: markdown, URLs, image refs, code blocks, @mentions, issue IDs, product names.",
            "",
        ]

        for issue in to_translate:
            issue_id = issue.get("idReadable", "?")
            summary = issue.get("summary", "")
            desc = issue.get("description", "") or ""

            lines.append(f"### {issue_id}")
            lines.append(f"SUMMARY: {summary}")
            if desc:
                lines.append(f"DESCRIPTION:\n{desc}")

            if include_comments:
                comments = issue.get("comments", [])
                for c in comments:
                    c_id = c.get("id", "?")
                    c_text = c.get("text", "")
                    c_author = c.get("author", {}).get("name", "?")
                    if c_text and _has_cyrillic(c_text):
                        lines.append(f"COMMENT {c_id} (by {c_author}):\n{c_text}")

            lines.append("---")
            lines.append("")

        return "\n".join(lines)

    @mcp.tool()
    async def apply_translations(
        translations: str,
        batch_tag: str = "",
        instance: str = "",
    ) -> str:
        """Apply translated text to YouTrack issues. Tags issues for rollback.

        Expected format per issue (separated by ---):
            ISSUE: AP-1554
            SUMMARY: Configure app logic for Oman timezone
            DESCRIPTION:
            As part of this task...
            COMMENT 4-15.91-12345:
            Verified on build 8.1.1, no objections...
            ---

        Fields not provided are left unchanged. DESCRIPTION and COMMENT can span multiple lines.

        Args:
            translations: Structured translation block (see format above)
            batch_tag: Optional batch tag for rollback. Auto-generated if empty.
            instance: YouTrack instance name (optional, for multi-instance setups)
        """
        client = resolver.resolve(instance)
        if not batch_tag:
            batch_tag = f"yt-translate-{int(time.time())}"

        # Parse the structured input
        blocks = translations.split("---")
        parsed = []
        for block in blocks:
            block = block.strip()
            if not block:
                continue

            entry: dict = {"comments": []}
            current_field = None
            current_lines: list[str] = []

            for line in block.split("\n"):
                if line.startswith("ISSUE:"):
                    if current_field and current_lines:
                        _save_field(entry, current_field, current_lines)
                    current_field = "issue"
                    current_lines = [line[len("ISSUE:"):].strip()]
                elif line.startswith("SUMMARY:"):
                    if current_field and current_lines:
                        _save_field(entry, current_field, current_lines)
                    current_field = "summary"
                    current_lines = [line[len("SUMMARY:"):].strip()]
                elif line.startswith("DESCRIPTION:"):
                    if current_field and current_lines:
                        _save_field(entry, current_field, current_lines)
                    current_field = "description"
                    rest = line[len("DESCRIPTION:"):].strip()
                    current_lines = [rest] if rest else []
                elif line.startswith("COMMENT "):
                    if current_field and current_lines:
                        _save_field(entry, current_field, current_lines)
                    # Extract comment ID: "COMMENT 4-15.91-12345:" or "COMMENT 4-15.91-12345 (by Author):"
                    match = re.match(r"COMMENT\s+([\w\-.]+)", line)
                    if match:
                        current_field = f"comment:{match.group(1)}"
                        rest_match = re.search(r":\s*(.*)", line)
                        rest = rest_match.group(1).strip() if rest_match else ""
                        current_lines = [rest] if rest else []
                    else:
                        current_lines.append(line)
                else:
                    current_lines.append(line)

            if current_field and current_lines:
                _save_field(entry, current_field, current_lines)

            if entry.get("issue"):
                parsed.append(entry)

        if not parsed:
            return "No translations found in input. Check the format."

        # Apply translations
        updated_issues = []
        updated_comments = 0
        errors = []

        for entry in parsed:
            issue_id = entry["issue"]
            try:
                # Tag for rollback
                await client.execute_command(issue_id, f"tag {batch_tag}")

                # Update summary and/or description
                payload: dict = {}
                if entry.get("summary"):
                    payload["summary"] = entry["summary"]
                if entry.get("description"):
                    payload["description"] = entry["description"]

                if payload:
                    await client.post(f"/api/issues/{issue_id}", json=payload)

                # Update comments
                for comment in entry.get("comments", []):
                    c_id = comment["id"]
                    c_text = comment["text"]
                    try:
                        await client.update_comment(issue_id, c_id, c_text)
                        updated_comments += 1
                    except (httpx.HTTPStatusError, ValueError) as e:
                        errors.append(f"{issue_id} comment {c_id}: {e}")

                # Add audit comment
                await client.post(
                    f"/api/issues/{issue_id}/comments",
                    json={"text": f"[yt-mcp] Translated to en-US. Batch: {batch_tag}"},
                )

                updated_issues.append(issue_id)

            except (httpx.HTTPStatusError, ValueError) as e:
                errors.append(f"{issue_id}: {e}")

        lines = ["## Translation applied"]
        lines.append(f"**Batch tag:** `{batch_tag}`")
        lines.append(f"**Issues updated:** {len(updated_issues)}")
        lines.append(f"**Comments updated:** {updated_comments}")
        if updated_issues:
            lines.append(f"**IDs:** {', '.join(updated_issues)}")
        if errors:
            lines.append(f"\n**Errors ({len(errors)}):**")
            for err in errors:
                lines.append(f"- {err}")
        lines.append("")
        lines.append(f"To undo: `bulk_rollback(batch_tag=\"{batch_tag}\")`")
        return "\n".join(lines)


def _save_field(entry: dict, field: str, lines: list[str]) -> None:
    """Save accumulated lines into the entry dict."""
    text = "\n".join(lines).strip()
    if not text:
        return
    if field == "issue":
        entry["issue"] = text
    elif field == "summary":
        entry["summary"] = text
    elif field == "description":
        entry["description"] = text
    elif field.startswith("comment:"):
        comment_id = field.split(":", 1)[1]
        entry["comments"].append({"id": comment_id, "text": text})
