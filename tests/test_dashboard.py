import time
from datetime import datetime, timezone

from yt_mcp.tools.dashboard import _parse_since, _compile_patterns, _should_exclude


class TestParseSince:
    def test_hours(self):
        result = _parse_since("24h")
        expected = int((time.time() - 24 * 3600) * 1000)
        assert abs(result - expected) < 2000  # within 2 seconds

    def test_days(self):
        result = _parse_since("7d")
        expected = int((time.time() - 7 * 86400) * 1000)
        assert abs(result - expected) < 2000

    def test_minutes(self):
        result = _parse_since("30m")
        expected = int((time.time() - 30 * 60) * 1000)
        assert abs(result - expected) < 2000

    def test_iso_date(self):
        result = _parse_since("2026-03-18")
        dt = datetime(2026, 3, 18, tzinfo=timezone.utc)
        expected = int(dt.timestamp() * 1000)
        assert result == expected

    def test_case_insensitive(self):
        result_lower = _parse_since("24h")
        result_upper = _parse_since("24H")
        assert abs(result_lower - result_upper) < 2000

    def test_whitespace_stripped(self):
        result = _parse_since("  7d  ")
        expected = int((time.time() - 7 * 86400) * 1000)
        assert abs(result - expected) < 2000

    def test_invalid_falls_back_to_24h(self):
        result = _parse_since("garbage")
        expected = int((time.time() - 86400) * 1000)
        assert abs(result - expected) < 2000


class TestCompilePatterns:
    def test_single_pattern(self):
        patterns = _compile_patterns("DevOps Daily")
        assert len(patterns) == 1
        assert patterns[0].search("DevOps Daily 19.03.26")

    def test_multiple_patterns(self):
        patterns = _compile_patterns("DevOps Daily,Report")
        assert len(patterns) == 2

    def test_empty_string(self):
        assert _compile_patterns("") == []

    def test_case_insensitive(self):
        patterns = _compile_patterns("devops daily")
        assert patterns[0].search("DevOps Daily 19.03.26")


class TestShouldExclude:
    def test_matches(self):
        patterns = _compile_patterns("DevOps Daily")
        issue = {"summary": "DevOps Daily 19.03.26"}
        assert _should_exclude(issue, patterns) is True

    def test_no_match(self):
        patterns = _compile_patterns("DevOps Daily")
        issue = {"summary": "Server audit"}
        assert _should_exclude(issue, patterns) is False

    def test_empty_patterns(self):
        issue = {"summary": "DevOps Daily 19.03.26"}
        assert _should_exclude(issue, []) is False
