from unittest.mock import AsyncMock, MagicMock

from yt_mcp.tools import translate
from yt_mcp.tools.translate import (
    _has_non_ascii,
    _is_bilingual,
    _save_field,
    _split_bilingual,
)


class TestHasNonAscii:
    def test_russian_text(self):
        assert _has_non_ascii("Привет мир") is True

    def test_english_text(self):
        assert _has_non_ascii("Hello world") is False

    def test_mixed_text(self):
        assert _has_non_ascii("Hello Привет") is True

    def test_empty_string(self):
        assert _has_non_ascii("") is False

    def test_ukrainian(self):
        assert _has_non_ascii("Київ") is True

    def test_numbers_only(self):
        assert _has_non_ascii("12345") is False

    def test_special_chars(self):
        assert _has_non_ascii("!@#$%") is False

    def test_chinese(self):
        assert _has_non_ascii("你好世界") is True

    def test_japanese(self):
        assert _has_non_ascii("こんにちは") is True

    def test_arabic(self):
        assert _has_non_ascii("مرحبا") is True

    def test_accented_latin(self):
        assert _has_non_ascii("café résumé") is True


class TestSaveField:
    def test_save_issue(self):
        entry = {"comments": []}
        _save_field(entry, "issue", ["PROJ-1554"])
        assert entry["issue"] == "PROJ-1554"

    def test_save_summary(self):
        entry = {"comments": []}
        _save_field(entry, "summary", ["Fix the login page"])
        assert entry["summary"] == "Fix the login page"

    def test_save_description_multiline(self):
        entry = {"comments": []}
        _save_field(entry, "description", ["Line 1", "Line 2", "Line 3"])
        assert entry["description"] == "Line 1\nLine 2\nLine 3"

    def test_save_comment(self):
        entry = {"comments": []}
        _save_field(entry, "comment:4-15.91-12345", ["Comment text here"])
        assert len(entry["comments"]) == 1
        assert entry["comments"][0]["id"] == "4-15.91-12345"
        assert entry["comments"][0]["text"] == "Comment text here"

    def test_save_empty_lines_ignored(self):
        entry = {"comments": []}
        _save_field(entry, "summary", ["", "", ""])
        assert "summary" not in entry

    def test_save_multiple_comments(self):
        entry = {"comments": []}
        _save_field(entry, "comment:id-1", ["First"])
        _save_field(entry, "comment:id-2", ["Second"])
        assert len(entry["comments"]) == 2


class TestTranslationParsing:
    """Test full block parsing by simulating the apply_translations parser logic."""

    def test_parse_single_block(self):
        import re
        block = (
            "ISSUE: PROJ-100\n"
            "SUMMARY: Translated title\n"
            "DESCRIPTION:\n"
            "Translated description line 1\n"
            "Line 2\n"
        )
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
                match = re.match(r"COMMENT\s+([\w\-.]+)", line)
                if match:
                    current_field = f"comment:{match.group(1)}"
                    current_lines = []
            else:
                current_lines.append(line)

        if current_field and current_lines:
            _save_field(entry, current_field, current_lines)

        assert entry["issue"] == "PROJ-100"
        assert entry["summary"] == "Translated title"
        assert "line 1" in entry["description"]
        assert "Line 2" in entry["description"]


class TestBlockSplitting:
    """Verify '---' splits between ISSUE blocks but not inside content (e.g. '----')."""

    @staticmethod
    def _split(translations: str) -> list[str]:
        """Mirror the parser's block-splitting logic."""
        raw_lines = translations.split("\n")
        blocks_lines: list[list[str]] = [[]]
        for idx, line in enumerate(raw_lines):
            if line.strip() == "---":
                next_non_empty = ""
                for look in raw_lines[idx + 1:]:
                    if look.strip():
                        next_non_empty = look.strip()
                        break
                if next_non_empty.startswith("ISSUE:"):
                    blocks_lines.append([])
                    continue
            blocks_lines[-1].append(line)
        return ["\n".join(b).strip() for b in blocks_lines if "\n".join(b).strip()]

    def test_separates_two_issues(self):
        text = (
            "ISSUE: A-1\nSUMMARY: First\n"
            "---\n"
            "ISSUE: B-2\nSUMMARY: Second\n"
        )
        blocks = self._split(text)
        assert len(blocks) == 2
        assert "A-1" in blocks[0]
        assert "B-2" in blocks[1]

    def test_four_dash_in_description_not_a_separator(self):
        """`----` inside a description must NOT split the block."""
        text = (
            "ISSUE: A-1\n"
            "SUMMARY: First\n"
            "DESCRIPTION:\n"
            "English text here\n"
            "\n"
            "----\n"
            "\n"
            "Russian text below\n"
        )
        blocks = self._split(text)
        assert len(blocks) == 1
        assert "English text" in blocks[0]
        assert "Russian text" in blocks[0]
        assert "----" in blocks[0]

    def test_three_dash_in_content_followed_by_non_issue_kept(self):
        """`---` followed by non-ISSUE content is treated as content."""
        text = (
            "ISSUE: A-1\n"
            "DESCRIPTION:\n"
            "Some content\n"
            "---\n"
            "More content (not a new issue)\n"
        )
        blocks = self._split(text)
        assert len(blocks) == 1
        assert "More content" in blocks[0]

    def test_three_dash_followed_by_issue_splits(self):
        text = (
            "ISSUE: A-1\nDESCRIPTION: first\n"
            "---\n"
            "ISSUE: B-2\nDESCRIPTION: second\n"
        )
        blocks = self._split(text)
        assert len(blocks) == 2


class TestSplitBilingual:
    """REGRESSION: split on standalone `----` line only — not on '----'
    appearing inside text. Whitespace adjacent to delimiter is stripped."""

    def test_standalone_delimiter(self):
        desc = "English here\n\n----\n\nРусский текст"
        top, bottom = _split_bilingual(desc)
        assert top == "English here"
        assert bottom == "Русский текст"

    def test_no_delimiter(self):
        top, bottom = _split_bilingual("Just English content")
        assert top == "Just English content"
        assert bottom == ""

    def test_empty_input(self):
        assert _split_bilingual("") == ("", "")

    def test_inline_dashes_not_split(self):
        """`text with ---- inline` should not be treated as the delimiter."""
        desc = "Step 1 ---- step 2 ---- step 3"
        top, bottom = _split_bilingual(desc)
        # No standalone delimiter line, so whole thing is top
        assert top == desc
        assert bottom == ""

    def test_custom_delimiter(self):
        desc = "Top\n\n===\n\nBottom"
        top, bottom = _split_bilingual(desc, "===")
        assert top == "Top"
        assert bottom == "Bottom"


class TestIsBilingual:
    """Detect descriptions already in EN + delimiter + RU bilingual format."""

    def test_real_bilingual(self):
        desc = "English summary here\n\n----\n\nРусский текст здесь"
        assert _is_bilingual(desc) is True

    def test_english_only(self):
        assert _is_bilingual("Just English content, no delimiter") is False

    def test_russian_only(self):
        assert _is_bilingual("Только русский текст") is False

    def test_delimiter_with_empty_bottom(self):
        desc = "Top content\n\n----\n\n"
        assert _is_bilingual(desc) is False

    def test_delimiter_with_empty_top(self):
        desc = "\n----\nBottom content"
        assert _is_bilingual(desc) is False

    def test_bilingual_with_english_below(self):
        """If bottom has no non-ASCII, it's not bilingual translation."""
        desc = "English top\n\n----\n\nMore English below"
        assert _is_bilingual(desc) is False

    def test_inline_dashes_not_bilingual(self):
        """Inline `----` mid-line doesn't count as delimiter."""
        desc = "Item 1 ---- item 2 — both Russian: тест"
        assert _is_bilingual(desc) is False

    def test_empty_or_none(self):
        assert _is_bilingual("") is False
        assert _is_bilingual(None or "") is False


# ---------- end-to-end via mock client ----------

def _register_translate_tools():
    """Spin up the translate tools with a captured mock client/resolver."""
    captured = {}
    client = MagicMock()
    client.get = AsyncMock()
    client.post = AsyncMock(return_value={})
    client.execute_command = AsyncMock()
    client.update_comment = AsyncMock(return_value={})
    resolver = MagicMock()
    resolver.resolve = MagicMock(return_value=client)

    tools = {}

    class FakeMcp:
        def tool(self):
            def decorator(fn):
                tools[fn.__name__] = fn
                return fn
            return decorator

    translate.register(FakeMcp(), resolver)
    captured["client"] = client
    captured["tools"] = tools
    return captured


class TestExcludeTranslated:
    """REGRESSION: `exclude_translated=True` (default) appends
    `tag: -yt-translate-*` to the query so prior batches don't reappear.
    Spares operators from maintaining a comma list of N batch tags."""

    def test_wildcard_appended_to_query(self):
        import asyncio
        ctx = _register_translate_tools()
        ctx["client"].get = AsyncMock(return_value=[])

        asyncio.run(ctx["tools"]["get_issues_for_translation"](
            query="project: ALPHA #Unresolved",
        ))

        called_params = ctx["client"].get.call_args.kwargs["params"]
        assert "tag: -yt-translate-*" in called_params["query"]

    def test_disabled_via_param(self):
        import asyncio
        ctx = _register_translate_tools()
        ctx["client"].get = AsyncMock(return_value=[])

        asyncio.run(ctx["tools"]["get_issues_for_translation"](
            query="project: ALPHA #Unresolved",
            exclude_translated=False,
        ))

        called_params = ctx["client"].get.call_args.kwargs["params"]
        assert "yt-translate" not in called_params["query"]

    def test_caller_tag_filter_respected(self):
        """If the caller supplied their own tag clause, don't auto-append."""
        import asyncio
        ctx = _register_translate_tools()
        ctx["client"].get = AsyncMock(return_value=[])

        asyncio.run(ctx["tools"]["get_issues_for_translation"](
            query="project: ALPHA tag: critical",
        ))

        called_params = ctx["client"].get.call_args.kwargs["params"]
        assert "yt-translate" not in called_params["query"]


class TestBilingualDetection:
    """REGRESSION: issues already in `EN + ---- + RU` format would be flagged
    for translation and, with `preserve_original=true`, get triple-content
    (EN + ---- + EN + ---- + RU). Now they're skipped at fetch time."""

    def test_skipped_when_already_bilingual(self):
        import asyncio
        ctx = _register_translate_tools()
        ctx["client"].get = AsyncMock(return_value=[
            {
                "idReadable": "ALPHA-1",
                "summary": "Already English title",
                "description": "English body\n\n----\n\nРусский текст",
            },
        ])

        out = asyncio.run(ctx["tools"]["get_issues_for_translation"](
            query="project: ALPHA",
            include_comments=False,
        ))
        assert "1 already bilingual" in out

    def test_translated_when_summary_still_russian(self):
        """Bilingual desc + Russian summary still needs translation."""
        import asyncio
        ctx = _register_translate_tools()
        ctx["client"].get = AsyncMock(return_value=[
            {
                "idReadable": "ALPHA-2",
                "summary": "Русское название",
                "description": "English body\n\n----\n\nРусский текст",
            },
        ])

        out = asyncio.run(ctx["tools"]["get_issues_for_translation"](
            query="project: ALPHA",
            include_comments=False,
        ))
        # Summary still needs translation, so issue IS included
        assert "ALPHA-2" in out
        assert "Issues to translate:** 1" in out


class TestPreserveOriginalSmartMerge:
    """REGRESSION: `preserve_original=true` against an already-bilingual
    description previously created EN/----/EN/----/RU triple content.
    Now it extracts the original-language portion and only the EN section
    is replaced."""

    def test_smart_merge_on_already_bilingual(self):
        import asyncio
        ctx = _register_translate_tools()
        # Return the same issue both for the initial fetch (in originals
        # gathering inside apply_translations) and for the post call.
        ctx["client"].get = AsyncMock(return_value={
            "description": "Old English\n\n----\n\nРусский текст",
        })

        block = (
            "ISSUE: ALPHA-1\n"
            "SUMMARY: New translated title\n"
            "DESCRIPTION:\nNew English content\n"
        )
        asyncio.run(ctx["tools"]["apply_translations"](
            translations=block,
            batch_tag="yt-translate-test",
            preserve_original=True,
        ))

        # Find the description POST call
        posts = [c for c in ctx["client"].post.call_args_list
                  if "ALPHA-1" in str(c.args) and "description" in str(c.kwargs)]
        assert posts, "Description POST should have been called"
        new_desc = posts[0].kwargs["json"]["description"]
        # Should contain the NEW English + delimiter + ORIGINAL Russian only
        assert "New English content" in new_desc
        assert "Русский текст" in new_desc
        # Should NOT contain the old English (was replaced)
        assert "Old English" not in new_desc
        # Delimiter must appear exactly once (top-level), not multiple times
        assert new_desc.count("\n----\n") == 1
