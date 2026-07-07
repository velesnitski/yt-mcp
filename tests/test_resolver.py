import pytest
from unittest.mock import MagicMock

from yt_mcp.config import YouTrackConfig
from yt_mcp.resolver import InstanceResolver


def _mock_client(url: str) -> MagicMock:
    cfg = YouTrackConfig(url=url, token="perm:test")
    mock = MagicMock()
    mock._config = cfg
    mock.base_url = cfg.url  # resolver uses the public property (ADR-024)
    return mock


class TestInstanceResolver:
    def test_single_instance_default(self):
        client = _mock_client("https://test.youtrack.cloud")
        resolver = InstanceResolver({"default": client})
        assert resolver.resolve() is client
        assert resolver.default_name == "default"
        assert resolver.instance_names == ["default"]
        assert resolver.is_multi is False

    def test_multi_instance(self):
        main = _mock_client("https://main.youtrack.cloud")
        second = _mock_client("https://second.youtrack.cloud")
        resolver = InstanceResolver({"main": main, "second": second})
        assert resolver.is_multi is True
        assert resolver.instance_names == ["main", "second"]

    def test_explicit_instance(self):
        main = _mock_client("https://main.youtrack.cloud")
        second = _mock_client("https://second.youtrack.cloud")
        resolver = InstanceResolver({"main": main, "second": second})
        assert resolver.resolve(instance="main") is main
        assert resolver.resolve(instance="second") is second

    def test_unknown_instance_raises(self):
        client = _mock_client("https://test.youtrack.cloud")
        resolver = InstanceResolver({"default": client})
        with pytest.raises(ValueError, match="Unknown YouTrack instance 'nope'"):
            resolver.resolve(instance="nope")

    def test_case_insensitive_instance_name(self):
        client = _mock_client("https://test.youtrack.cloud")
        resolver = InstanceResolver({"main": client})
        assert resolver.resolve(instance="MAIN") is client
        assert resolver.resolve(instance="Main") is client

    def test_domain_substring_match(self):
        # LLM guesses subdomain but instance has a different name
        main = _mock_client("https://main.youtrack.cloud")
        second = _mock_client("https://alpha.youtrack.cloud")
        resolver = InstanceResolver({"main": main, "second": second})
        assert resolver.resolve(instance="alpha") is second
        assert resolver.resolve(instance="ALPHA") is second  # case-insensitive

    def test_exact_match_wins_over_substring(self):
        # If "second" is a real instance name, don't fall through to substring match
        main = _mock_client("https://main.youtrack.cloud")
        second = _mock_client("https://second.youtrack.cloud")
        resolver = InstanceResolver({"main": main, "second": second})
        assert resolver.resolve(instance="second") is second

    def test_url_auto_detection(self):
        main = _mock_client("https://main.youtrack.cloud")
        second = _mock_client("https://second.youtrack.cloud")
        resolver = InstanceResolver({"main": main, "second": second})

        # URL from main instance
        assert resolver.resolve(
            identifier="https://main.youtrack.cloud/issue/PROJ-123/slug"
        ) is main

        # URL from second instance
        assert resolver.resolve(
            identifier="https://second.youtrack.cloud/issue/APP-456"
        ) is second

    def test_url_unknown_domain_falls_back_to_default(self):
        main = _mock_client("https://main.youtrack.cloud")
        second = _mock_client("https://second.youtrack.cloud")
        resolver = InstanceResolver({"main": main, "second": second})

        # Unknown domain → default (first)
        assert resolver.resolve(
            identifier="https://unknown.youtrack.cloud/issue/X-1"
        ) is main

    def test_plain_id_uses_default(self):
        main = _mock_client("https://main.youtrack.cloud")
        second = _mock_client("https://second.youtrack.cloud")
        resolver = InstanceResolver({"main": main, "second": second})

        # Plain ID without URL → default
        assert resolver.resolve(identifier="PROJ-123") is main

    def test_explicit_instance_overrides_url(self):
        main = _mock_client("https://main.youtrack.cloud")
        second = _mock_client("https://second.youtrack.cloud")
        resolver = InstanceResolver({"main": main, "second": second})

        # Explicit instance wins over URL auto-detection
        assert resolver.resolve(
            instance="second",
            identifier="https://main.youtrack.cloud/issue/PROJ-123"
        ) is second

    def test_empty_clients_raises(self):
        with pytest.raises(ValueError, match="At least one"):
            InstanceResolver({})

    def test_default_is_first_instance(self):
        a = _mock_client("https://a.youtrack.cloud")
        b = _mock_client("https://b.youtrack.cloud")
        resolver = InstanceResolver({"alpha": a, "beta": b})
        assert resolver.default_name == "alpha"
        assert resolver.resolve() is a
