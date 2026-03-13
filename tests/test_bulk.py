from yt_mcp.tools.bulk import _validate_batch_tag, _BATCH_TAG_RE


class TestBatchTagValidation:
    def test_valid_mcp_tag(self):
        assert _validate_batch_tag("yt-mcp-1741794000") is None

    def test_valid_translate_tag(self):
        assert _validate_batch_tag("yt-translate-1741794000") is None

    def test_valid_long_timestamp(self):
        assert _validate_batch_tag("yt-mcp-17417940001234") is None

    def test_rejects_short_timestamp(self):
        result = _validate_batch_tag("yt-mcp-123")
        assert result is not None
        assert "Invalid" in result

    def test_rejects_arbitrary_string(self):
        assert _validate_batch_tag("hello-world") is not None

    def test_rejects_injection_attempt(self):
        assert _validate_batch_tag("yt-mcp-1741794000; rm -rf /") is not None

    def test_rejects_wrong_prefix(self):
        assert _validate_batch_tag("yt-bulk-1741794000") is not None

    def test_rejects_empty(self):
        assert _validate_batch_tag("") is not None

    def test_rejects_no_timestamp(self):
        assert _validate_batch_tag("yt-mcp-") is not None


class TestBatchTagRegex:
    def test_matches_mcp(self):
        assert _BATCH_TAG_RE.match("yt-mcp-1741794000")

    def test_matches_translate(self):
        assert _BATCH_TAG_RE.match("yt-translate-1741794000")

    def test_no_match_extra_chars(self):
        assert not _BATCH_TAG_RE.match("yt-mcp-1741794000abc")

    def test_no_match_spaces(self):
        assert not _BATCH_TAG_RE.match("yt-mcp-1741794000 ")
