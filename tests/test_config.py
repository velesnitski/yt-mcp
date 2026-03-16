import os
from unittest.mock import patch

from yt_mcp.config import load_config, load_all_configs


def _env(**kwargs):
    """Helper to set env vars for testing."""
    base = {"YOUTRACK_URL": "", "YOUTRACK_TOKEN": "", "YOUTRACK_INSTANCES": ""}
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


class TestLoadAllConfigs:
    def test_single_instance_backward_compat(self):
        """No YOUTRACK_INSTANCES → single 'default' instance."""
        with _env(YOUTRACK_URL="https://test.youtrack.cloud", YOUTRACK_TOKEN="perm:abc"):
            configs = load_all_configs()
            assert list(configs.keys()) == ["default"]
            assert configs["default"].url == "https://test.youtrack.cloud"
            assert configs["default"].token == "perm:abc"

    def test_multi_instance(self):
        with _env(
            YOUTRACK_INSTANCES="main,second",
            YOUTRACK_MAIN_URL="https://main.youtrack.cloud",
            YOUTRACK_MAIN_TOKEN="perm:main",
            YOUTRACK_SECOND_URL="https://second.youtrack.cloud",
            YOUTRACK_SECOND_TOKEN="perm:second",
        ):
            configs = load_all_configs()
            assert list(configs.keys()) == ["main", "second"]
            assert configs["main"].url == "https://main.youtrack.cloud"
            assert configs["main"].token == "perm:main"
            assert configs["second"].url == "https://second.youtrack.cloud"
            assert configs["second"].token == "perm:second"

    def test_first_instance_fallback_to_unprefixed(self):
        """First instance uses YOUTRACK_URL/YOUTRACK_TOKEN as fallback."""
        with _env(
            YOUTRACK_INSTANCES="main,second",
            YOUTRACK_URL="https://fallback.youtrack.cloud",
            YOUTRACK_TOKEN="perm:fallback",
            YOUTRACK_SECOND_URL="https://second.youtrack.cloud",
            YOUTRACK_SECOND_TOKEN="perm:second",
        ):
            configs = load_all_configs()
            assert configs["main"].url == "https://fallback.youtrack.cloud"
            assert configs["main"].token == "perm:fallback"
            assert configs["second"].url == "https://second.youtrack.cloud"

    def test_second_instance_no_fallback(self):
        """Second instance does NOT fall back to unprefixed vars."""
        with _env(
            YOUTRACK_INSTANCES="main,second",
            YOUTRACK_MAIN_URL="https://main.youtrack.cloud",
            YOUTRACK_MAIN_TOKEN="perm:main",
            YOUTRACK_URL="https://fallback.youtrack.cloud",
            YOUTRACK_TOKEN="perm:fallback",
        ):
            configs = load_all_configs()
            assert configs["second"].url == ""
            assert configs["second"].token == ""

    def test_global_settings_shared(self):
        with _env(
            YOUTRACK_INSTANCES="a,b",
            YOUTRACK_A_URL="https://a.youtrack.cloud",
            YOUTRACK_A_TOKEN="perm:a",
            YOUTRACK_B_URL="https://b.youtrack.cloud",
            YOUTRACK_B_TOKEN="perm:b",
            YOUTRACK_READ_ONLY="true",
            DISABLED_TOOLS="delete_issue",
        ):
            configs = load_all_configs()
            assert configs["a"].read_only is True
            assert configs["b"].read_only is True
            assert "delete_issue" in configs["a"].disabled_tools
            assert "delete_issue" in configs["b"].disabled_tools

    def test_empty_instances_string(self):
        with _env(YOUTRACK_INSTANCES="", YOUTRACK_URL="https://test.youtrack.cloud", YOUTRACK_TOKEN="perm:t"):
            configs = load_all_configs()
            assert list(configs.keys()) == ["default"]
