from yt_mcp.tools.translate import _has_non_ascii, _save_field


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
