import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class YouTrackConfig:
    url: str
    token: str
    read_only: bool = False
    disabled_tools: frozenset[str] = field(default_factory=frozenset)
    max_bulk_results: int = 100


def _validate_url(url: str) -> str:
    """Validate URL scheme. Returns empty string if blocked."""
    if url and not url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
        import sys
        print(
            f"WARNING: YOUTRACK_URL ({url}) does not use HTTPS. "
            "Set YOUTRACK_ALLOW_HTTP=1 to allow insecure connections.",
            file=sys.stderr,
        )
        if not os.environ.get("YOUTRACK_ALLOW_HTTP"):
            return ""
    return url


def _parse_global_settings() -> tuple[bool, frozenset, int]:
    """Parse server-level settings shared across all instances."""
    read_only = os.environ.get("YOUTRACK_READ_ONLY", "").lower() in ("1", "true", "yes")

    disabled_raw = os.environ.get("DISABLED_TOOLS", "")
    disabled = frozenset(
        t.strip().lower().replace("-", "_")
        for t in disabled_raw.split(",")
        if t.strip()
    )

    try:
        max_bulk = int(os.environ.get("YOUTRACK_MAX_BULK_RESULTS", "100"))
    except ValueError:
        max_bulk = 100
    return read_only, disabled, max_bulk


def load_config() -> YouTrackConfig:
    url = _validate_url(os.environ.get("YOUTRACK_URL", "").rstrip("/"))
    read_only, disabled, max_bulk = _parse_global_settings()

    return YouTrackConfig(
        url=url,
        token=os.environ.get("YOUTRACK_TOKEN", ""),
        read_only=read_only,
        disabled_tools=disabled,
        max_bulk_results=max_bulk,
    )


def load_all_configs() -> dict[str, YouTrackConfig]:
    """Load configs for all YouTrack instances.

    Backward compatible: if YOUTRACK_INSTANCES is not set, returns a single
    'default' instance using YOUTRACK_URL / YOUTRACK_TOKEN.

    Multi-instance example:
        YOUTRACK_INSTANCES=main,second
        YOUTRACK_MAIN_URL=https://main.youtrack.cloud
        YOUTRACK_MAIN_TOKEN=perm:xxx
        YOUTRACK_SECOND_URL=https://second.youtrack.cloud
        YOUTRACK_SECOND_TOKEN=perm:yyy

    The first instance falls back to unprefixed YOUTRACK_URL / YOUTRACK_TOKEN
    if its prefixed vars are not set.
    """
    instances_raw = os.environ.get("YOUTRACK_INSTANCES", "")

    if not instances_raw:
        return {"default": load_config()}

    instances = [i.strip() for i in instances_raw.split(",") if i.strip()]
    if not instances:
        return {"default": load_config()}

    read_only, disabled, max_bulk = _parse_global_settings()

    configs: dict[str, YouTrackConfig] = {}
    for i, name in enumerate(instances):
        prefix = name.upper()

        url = os.environ.get(f"YOUTRACK_{prefix}_URL", "")
        if not url and i == 0:
            url = os.environ.get("YOUTRACK_URL", "")
        url = _validate_url(url.rstrip("/"))

        token = os.environ.get(f"YOUTRACK_{prefix}_TOKEN", "")
        if not token and i == 0:
            token = os.environ.get("YOUTRACK_TOKEN", "")

        configs[name] = YouTrackConfig(
            url=url,
            token=token,
            read_only=read_only,
            disabled_tools=disabled,
            max_bulk_results=max_bulk,
        )

    return configs
