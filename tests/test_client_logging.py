"""Tests for client error handling and Sentry scrubbing."""

import logging

import httpx
import pytest

from yt_mcp.client import YouTrackClient
from yt_mcp.config import YouTrackConfig
from yt_mcp.logging import _scrub_event


def _make_client() -> YouTrackClient:
    cfg = YouTrackConfig(token="test-token", url="https://example.invalid")
    return YouTrackClient(cfg)


def _make_response(status_code: int, body: dict) -> httpx.Response:
    req = httpx.Request("GET", "https://example.invalid/api/issues?query=x")
    return httpx.Response(status_code=status_code, json=body, request=req)


class TestClientErrorLogLevel:
    @pytest.mark.asyncio
    async def test_400_logs_as_warning_not_error(self, caplog):
        """400 = caller's bad query; must not surface as ERROR (would page Sentry)."""
        client = _make_client()
        resp = _make_response(400, {"error_description": "Can't parse search query"})
        with caplog.at_level(logging.DEBUG, logger="yt_mcp"):
            with pytest.raises(ValueError, match="YouTrack query error \\(400\\)"):
                await client._handle_error(resp)
        records = [r for r in caplog.records if r.name == "yt_mcp"]
        assert records, "expected a yt_mcp log record"
        assert all(r.levelno <= logging.WARNING for r in records)
        assert any(r.levelno == logging.WARNING for r in records)

    @pytest.mark.asyncio
    async def test_404_logs_as_warning_not_error(self, caplog):
        client = _make_client()
        resp = _make_response(404, {"error_description": "Issue not found"})
        with caplog.at_level(logging.DEBUG, logger="yt_mcp"):
            with pytest.raises(ValueError, match="YouTrack not found error \\(404\\)"):
                await client._handle_error(resp)
        records = [r for r in caplog.records if r.name == "yt_mcp"]
        assert records
        assert all(r.levelno <= logging.WARNING for r in records)


class TestScrubEventLogentryFallback:
    """Even when exc_info is absent (log-only path), drop user-input events."""

    def test_drops_event_with_query_error_in_logentry_message(self):
        event = {
            "logentry": {
                "formatted": "YouTrack query error (400): Can't parse search query",
                "message": "YouTrack query error (400): Can't parse search query",
            },
            "extra": {},
        }
        assert _scrub_event(event, {}) is None

    def test_drops_event_with_not_found_in_logentry(self):
        event = {
            "logentry": {"formatted": "YouTrack not found error (404): nope"},
            "extra": {},
        }
        assert _scrub_event(event, {}) is None

    def test_drops_event_with_unknown_instance_in_logentry(self):
        event = {
            "logentry": {"formatted": "Unknown YouTrack instance: ghost"},
            "extra": {},
        }
        assert _scrub_event(event, {}) is None

    def test_keeps_unrelated_error_event(self):
        event = {
            "logentry": {"formatted": "Disk full"},
            "extra": {},
        }
        assert _scrub_event(event, {}) is event

    def test_keeps_event_with_no_logentry(self):
        event = {"extra": {}}
        assert _scrub_event(event, {}) is event

    def test_falls_back_to_message_field_when_no_formatted(self):
        event = {
            "logentry": {"message": "YouTrack query error (400): bad"},
            "extra": {},
        }
        assert _scrub_event(event, {}) is None

    def test_still_redacts_extras_when_event_kept(self):
        event = {
            "logentry": {"formatted": "Disk full"},
            "extra": {"db_password": "secret", "ok_field": "fine"},
        }
        result = _scrub_event(event, {})
        assert result is event
        assert result["extra"]["db_password"] == "[REDACTED]"
        assert result["extra"]["ok_field"] == "fine"
