from urllib.parse import urlparse

from yt_mcp.client import YouTrackClient


class InstanceResolver:
    """Resolves which YouTrack client to use for a given request.

    Priority:
    1. Explicit instance name parameter
    2. Auto-detect from URL domain in the identifier
    3. Default (first configured) instance
    """

    def __init__(self, clients: dict[str, YouTrackClient]):
        if not clients:
            raise ValueError("At least one YouTrack instance must be configured.")
        self._clients = clients
        self._default = next(iter(clients))

        self._domain_map: dict[str, str] = {}
        for name, client in clients.items():
            domain = urlparse(client._config.url).hostname
            if domain:
                self._domain_map[domain] = name

    def resolve(self, instance: str = "", identifier: str = "") -> YouTrackClient:
        """Pick the right client based on instance name or URL auto-detection."""
        if instance:
            # Exact match
            if instance in self._clients:
                return self._clients[instance]
            # Case-insensitive match
            lower = instance.lower()
            for name in self._clients:
                if name.lower() == lower:
                    return self._clients[name]
            # Domain substring match (e.g. 'alpha' matches 'alpha.youtrack.cloud')
            for domain, name in self._domain_map.items():
                if lower in domain.lower():
                    return self._clients[name]
            available = ", ".join(self._clients.keys())
            raise ValueError(
                f"Unknown YouTrack instance '{instance}'. "
                f"Available: {available}"
            )

        if identifier and "://" in identifier:
            domain = urlparse(identifier).hostname
            if domain and domain in self._domain_map:
                return self._clients[self._domain_map[domain]]

        return self._clients[self._default]

    @property
    def default_name(self) -> str:
        return self._default

    @property
    def instance_names(self) -> list[str]:
        return list(self._clients.keys())

    @property
    def is_multi(self) -> bool:
        return len(self._clients) > 1
