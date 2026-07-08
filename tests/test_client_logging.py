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


# --- Query auto-rewrite preprocessing (v1.12.0) ---

from yt_mcp.client import _preprocess_query_params


class TestPreprocessQueryParams:
    def test_none_passes_through(self):
        assert _preprocess_query_params(None) is None

    def test_params_without_query_unchanged(self):
        params = {"fields": "id,summary", "$top": "10"}
        assert _preprocess_query_params(params) is params

    def test_non_string_query_unchanged(self):
        # Defensive: if a caller somehow passes a non-str query, don't crash
        params = {"query": 42}
        assert _preprocess_query_params(params) is params

    def test_clean_query_returns_same_dict(self):
        params = {"query": "project: ALPHA #Unresolved", "fields": "id"}
        result = _preprocess_query_params(params)
        # No rewrite needed → returned as-is (same object, not copied)
        assert result is params

    def test_or_query_rewritten(self, caplog):
        params = {"query": "summary: foo OR summary: bar"}
        with caplog.at_level(logging.INFO, logger="yt_mcp"):
            result = _preprocess_query_params(params)
        # Modified copy, original untouched
        assert result is not params
        assert result["query"] == "summary: foo, bar"
        assert params["query"] == "summary: foo OR summary: bar"  # unmutated
        # Logged at info level
        msgs = [r.getMessage() for r in caplog.records if r.name == "yt_mcp"]
        assert any("auto-rewrite" in m.lower() for m in msgs)

    def test_braces_in_query_bypass_rewrite(self):
        # Brace-protected state lists must not be rewritten
        params = {"query": "State: {For Review} OR State: {Ready for Test}"}
        result = _preprocess_query_params(params)
        assert result is params  # unchanged


# --- get_instance_url + get_current_user JSON shape (v1.12.0) ---

import json
from unittest.mock import AsyncMock, MagicMock

from mcp.server.fastmcp import FastMCP

from yt_mcp.resolver import InstanceResolver
from yt_mcp.tools.users import register as _register_users


def _mock_users_mcp(me_response: dict, base_url: str = "https://example.invalid"):
    mcp = FastMCP("test")
    client = MagicMock()
    client.get = AsyncMock(return_value=me_response)
    client.base_url = base_url
    resolver = MagicMock(spec=InstanceResolver)
    resolver.resolve = MagicMock(return_value=client)
    _register_users(mcp, resolver)
    return mcp, client


def _tool_fn(mcp, name):
    return mcp._tool_manager._tools[name].fn


class TestGetInstanceUrl:
    @pytest.mark.asyncio
    async def test_report_returns_plain_url(self):
        mcp, client = _mock_users_mcp({}, base_url="https://acme.youtrack.cloud")
        out = await _tool_fn(mcp, "get_instance_url")()
        assert out == "https://acme.youtrack.cloud"

    @pytest.mark.asyncio
    async def test_json_returns_wrapped_dict(self):
        mcp, client = _mock_users_mcp({}, base_url="https://acme.youtrack.cloud")
        out = await _tool_fn(mcp, "get_instance_url")(format="json")
        parsed = json.loads(out)
        assert parsed == {"base_url": "https://acme.youtrack.cloud"}

    @pytest.mark.asyncio
    async def test_no_api_call_made(self):
        # The point of this tool is it's free — no auth needed
        mcp, client = _mock_users_mcp({}, base_url="https://x.invalid")
        await _tool_fn(mcp, "get_instance_url")()
        client.get.assert_not_called()


class TestGetCurrentUserJson:
    @pytest.mark.asyncio
    async def test_json_includes_instance_url(self):
        me = {
            "id": "1-1", "login": "alice", "fullName": "Alice A",
            "email": "alice@example.invalid", "online": True, "banned": False,
            "avatarUrl": "/hub/api/rest/avatar/1-1",
        }
        mcp, client = _mock_users_mcp(me, base_url="https://acme.youtrack.cloud")
        out = await _tool_fn(mcp, "get_current_user")(format="json")
        parsed = json.loads(out)
        assert parsed["login"] == "alice"
        assert parsed["name"] == "Alice A"
        assert parsed["email"] == "alice@example.invalid"
        assert parsed["instance_url"] == "https://acme.youtrack.cloud"
        assert parsed["online"] is True
        assert parsed["banned"] is False

    @pytest.mark.asyncio
    async def test_report_default_includes_instance_line(self):
        me = {"id": "1-1", "login": "bob", "fullName": "Bob B"}
        mcp, client = _mock_users_mcp(me, base_url="https://acme.youtrack.cloud")
        out = await _tool_fn(mcp, "get_current_user")()  # default format=report
        assert "Bob B" in out
        assert "https://acme.youtrack.cloud" in out
        assert out.startswith("## Current user")


def test_log_handlers_rotate(monkeypatch, tmp_path):
    """Long-lived installs must not grow logs unboundedly (ADR-026)."""
    import importlib, logging as _logging
    import yt_mcp.logging as ytlog
    monkeypatch.setenv("YOUTRACK_LOG_FILE", str(tmp_path / "err.log"))
    monkeypatch.setenv("YOUTRACK_ANALYTICS_FILE", str(tmp_path / "an.log"))
    # fresh logger names so we don't double-register on the shared root
    logger = ytlog.setup_logging()
    rotating = [h for h in logger.handlers
                if isinstance(h, _logging.handlers.RotatingFileHandler)]
    assert rotating, "error log must use RotatingFileHandler"
    analytics = _logging.getLogger("yt_mcp.analytics")
    assert any(isinstance(h, _logging.handlers.RotatingFileHandler)
               for h in analytics.handlers), "analytics log must rotate"
