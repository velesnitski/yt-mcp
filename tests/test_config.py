import os
from unittest.mock import patch

from yt_mcp.config import load_config


def _env(**kwargs):
    """Helper to set env vars for testing."""
    base = {"YOUTRACK_URL": "", "YOUTRACK_TOKEN": ""}
    base.update(kwargs)
    return patch.dict(os.environ, base, clear=False)


class TestLoadConfig:
    def test_basic_config(self):
        with _env(YOUTRACK_URL="https://test.youtrack.cloud", YOUTRACK_TOKEN="perm:abc"):
            cfg = load_config()
            assert cfg.url == "https://test.youtrack.cloud"
            assert cfg.token == "perm:abc"
            assert cfg.read_only is False
            assert cfg.disabled_tools == frozenset()
            assert cfg.max_bulk_results == 100

    def test_url_trailing_slash_stripped(self):
        with _env(YOUTRACK_URL="https://test.youtrack.cloud/"):
            cfg = load_config()
            assert cfg.url == "https://test.youtrack.cloud"

    def test_empty_url(self):
        with _env(YOUTRACK_URL=""):
            cfg = load_config()
            assert cfg.url == ""

    def test_http_url_blocked_by_default(self):
        with _env(YOUTRACK_URL="http://evil.com"):
            cfg = load_config()
            assert cfg.url == ""

    def test_http_localhost_allowed(self):
        with _env(YOUTRACK_URL="http://localhost:8080"):
            cfg = load_config()
            assert cfg.url == "http://localhost:8080"

    def test_http_127_allowed(self):
        with _env(YOUTRACK_URL="http://127.0.0.1:8080"):
            cfg = load_config()
            assert cfg.url == "http://127.0.0.1:8080"

    def test_http_allowed_with_flag(self):
        with _env(YOUTRACK_URL="http://internal.corp:8080", YOUTRACK_ALLOW_HTTP="1"):
            cfg = load_config()
            assert cfg.url == "http://internal.corp:8080"

    def test_read_only_true(self):
        for val in ("1", "true", "yes", "True", "YES"):
            with _env(YOUTRACK_READ_ONLY=val):
                cfg = load_config()
                assert cfg.read_only is True, f"Failed for '{val}'"

    def test_read_only_false(self):
        for val in ("", "0", "false", "no"):
            with _env(YOUTRACK_READ_ONLY=val):
                cfg = load_config()
                assert cfg.read_only is False, f"Failed for '{val}'"

    def test_disabled_tools_parsing(self):
        with _env(DISABLED_TOOLS="delete_issue, Bulk-Update-Execute, CREATE_ISSUE"):
            cfg = load_config()
            assert cfg.disabled_tools == frozenset({
                "delete_issue",
                "bulk_update_execute",
                "create_issue",
            })

    def test_disabled_tools_empty(self):
        with _env(DISABLED_TOOLS=""):
            cfg = load_config()
            assert cfg.disabled_tools == frozenset()

    def test_max_bulk_results(self):
        with _env(YOUTRACK_MAX_BULK_RESULTS="50"):
            cfg = load_config()
            assert cfg.max_bulk_results == 50

    def test_config_is_frozen(self):
        with _env(YOUTRACK_URL="https://test.youtrack.cloud", YOUTRACK_TOKEN="perm:abc"):
            cfg = load_config()
            try:
                cfg.url = "https://other.com"  # type: ignore
                assert False, "Should have raised"
            except AttributeError:
                pass
