import re
import time

import httpx

from yt_mcp.resolver import InstanceResolver


_NON_ASCII_RE = re.compile(r"[^\x00-\x7F]")
_COMMENT_ID_RE = re.compile(r"COMMENT\s+([\w\-.]+)")
_COMMENT_REST_RE = re.compile(r":\s*(.*)")


def _has_non_ascii(text: str) -> bool:
    """Check if text contains non-ASCII characters (non-English text)."""
    return bool(_NON_ASCII_RE.search(text))


def _split_bilingual(desc: str, delimiter: str = "----") -> tuple[str, str]:
    """Split a description on a standalone delimiter line.

    Returns ``(top, bottom)``. If no standalone delimiter line is present,
    returns ``(desc, "")``. Whitespace adjacent to the delimiter is stripped.
    """
    if not desc:
        return "", ""
    lines = desc.split("\n")
    for i, line in enumerate(lines):
        if line.strip() == delimiter:
            top = "\n".join(lines[:i]).rstrip()
            bottom = "\n".join(lines[i + 1:]).lstrip()
            return top, bottom
    return desc, ""


def _is_bilingual(desc: str, delimiter: str = "----") -> bool:
    """True if description appears to already be in EN + delimiter + RU format.

    Heuristic: a standalone delimiter line with non-empty content above and
    below, where the bottom portion contains non-ASCII characters. False
    positives just trigger an unnecessary skip — never data loss.
    """
    top, bottom = _split_bilingual(desc, delimiter)
    if not top.strip() or not bottom.strip():
        return False
    return _has_non_ascii(bottom)


def register(mcp, resolver: InstanceResolver):

    @mcp.tool()
    async def get_issues_for_translation(
        query: str,
        include_comments: bool = True,
        max_results: int = 10,
        exclude_translated: bool = True,
        delimiter: str = "----",
        instance: str = "",
    ) -> str:
        """Fetch issues with non-ASCII text for translation. Call apply_translations with results.

        Args:
            query: YouTrack search query
            include_comments: Include comments (default: True)
            max_results: Batch size (default: 10)
            exclude_translated: Auto-exclude issues already tagged from prior
                runs (any `yt-translate-*` tag) and issues whose description
                already has the EN + delimiter + original-language bilingual
                structure. Default True. Set False to force re-translation.
            delimiter: Bilingual delimiter to detect already-translated state
                (default: '----'). Must match what `apply_translations` uses.
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance)

        # Auto-exclude previously-tagged issues unless the caller already has
        # an explicit tag clause. Wildcard `tag: -yt-translate-*` is supported
        # by YouTrack tag-query syntax.
        if exclude_translated and "tag:" not in query.lower():
            query = f"{query} tag: -yt-translate-*".strip()

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

        # Filter to issues with non-ASCII text that aren't already bilingual.
        to_translate = []
        skipped_english = 0
        skipped_bilingual = 0
        for issue in issues:
            summary = issue.get("summary", "")
            desc = issue.get("description", "") or ""
            summary_needs = _has_non_ascii(summary)
            desc_needs = _has_non_ascii(desc)
            if not summary_needs and not desc_needs:
                skipped_english += 1
                continue
            # If summary is already English and the description is already
            # bilingual, skip — it's in the desired final format.
            if (
                exclude_translated
                and not summary_needs
                and desc_needs
                and _is_bilingual(desc, delimiter)
            ):
                skipped_bilingual += 1
                continue
            to_translate.append(issue)

        if not to_translate:
            total_skipped = skipped_english + skipped_bilingual
            if skipped_bilingual:
                return (
                    f"No issues need translation (all {total_skipped} skipped: "
                    f"{skipped_english} already English, {skipped_bilingual} already bilingual)."
                )
            return f"No issues need translation (all {total_skipped} are already in target language)."

        # Build structured output for the LLM to translate
        skipped_label = f"{skipped_english + skipped_bilingual} already in target language"
        if skipped_bilingual:
            skipped_label = (
                f"{skipped_english + skipped_bilingual} already done "
                f"({skipped_english} English, {skipped_bilingual} bilingual)"
            )
        lines = [
            "## Issues for translation",
            f"**Query:** `{query}`",
            f"**Issues to translate:** {len(to_translate)} (skipped {skipped_label})",
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
                    c_author = (c.get("author") or {}).get("name", "?")
                    if c_text and _has_non_ascii(c_text):
                        lines.append(f"COMMENT {c_id} (by {c_author}):\n{c_text}")

            lines.append("---")
            lines.append("")

        return "\n".join(lines)

    @mcp.tool()
    async def apply_translations(
        translations: str,
        batch_tag: str = "",
        preserve_original: bool = False,
        delimiter: str = "----",
        instance: str = "",
    ) -> str:
        """Apply translated text to YouTrack issues. Tags for rollback. Format: ISSUE/SUMMARY/DESCRIPTION/COMMENT blocks separated by ---.

        Args:
            translations: Structured translation block
            batch_tag: Batch tag for rollback (auto-generated if empty)
            preserve_original: If True, append the current original description below
                a delimiter so both languages remain visible. Summary is still replaced.
                Comments use the same bilingual format.
            delimiter: Separator line between English and original (default: '----')
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance)
        if not batch_tag:
            batch_tag = f"yt-translate-{int(time.time())}"

        # Parse the structured input.
        # Split on standalone '---' separator lines, BUT only when the next
        # non-empty line starts with 'ISSUE:' — otherwise treat as content
        # (e.g. '----' delimiter inside a bilingual description).
        raw_lines = translations.split("\n")
        blocks_lines: list[list[str]] = [[]]
        for idx, line in enumerate(raw_lines):
            stripped = line.strip()
            if stripped == "---":
                # Look ahead for the next non-empty line
                next_non_empty = ""
                for look in raw_lines[idx + 1:]:
                    if look.strip():
                        next_non_empty = look.strip()
                        break
                if next_non_empty.startswith("ISSUE:"):
                    blocks_lines.append([])
                    continue
            blocks_lines[-1].append(line)
        blocks = ["\n".join(b) for b in blocks_lines]
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
                    match = _COMMENT_ID_RE.match(line)
                    if match:
                        current_field = f"comment:{match.group(1)}"
                        rest_match = _COMMENT_REST_RE.search(line)
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

                # Fetch originals if we need to preserve them
                originals: dict = {}
                if preserve_original:
                    fields = "description"
                    if entry.get("comments"):
                        fields += ",comments(id,text)"
                    cur = await client.get(
                        f"/api/issues/{issue_id}",
                        params={"fields": fields},
                    )
                    originals["description"] = cur.get("description", "") or ""
                    originals["comments"] = {
                        c.get("id"): c.get("text", "")
                        for c in (cur.get("comments") or [])
                    }

                # Update summary and/or description
                payload: dict = {}
                if entry.get("summary"):
                    payload["summary"] = entry["summary"]
                if entry.get("description"):
                    new_desc = entry["description"]
                    if preserve_original and originals.get("description"):
                        orig_desc = originals["description"]
                        if _is_bilingual(orig_desc, delimiter):
                            # Already bilingual — replace just the EN section,
                            # keep the original-language portion verbatim.
                            # Prevents EN/----/EN/----/RU triple-content.
                            _, ru_part = _split_bilingual(orig_desc, delimiter)
                            new_desc = f"{new_desc}\n\n{delimiter}\n\n{ru_part}"
                        else:
                            new_desc = (
                                f"{new_desc}\n\n{delimiter}\n\n{orig_desc}"
                            )
                    payload["description"] = new_desc

                if payload:
                    await client.post(f"/api/issues/{issue_id}", json=payload)

                # Update comments
                for comment in entry.get("comments", []):
                    c_id = comment["id"]
                    c_text = comment["text"]
                    if preserve_original:
                        orig_text = (originals.get("comments") or {}).get(c_id, "")
                        if orig_text:
                            c_text = f"{c_text}\n\n{delimiter}\n\n{orig_text}"
                    try:
                        await client.update_comment(issue_id, c_id, c_text)
                        updated_comments += 1
                    except (httpx.HTTPStatusError, ValueError) as e:
                        errors.append(f"{issue_id} comment {c_id}: {e}")

                # Add audit comment
                await client.post(
                    f"/api/issues/{issue_id}/comments",
                    json={"text": f"[yt-mcp] Translated. Batch: {batch_tag}"},
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
