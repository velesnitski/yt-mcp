import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class YouTrackConfig:
    url: str
    token: str
    read_only: bool = False
    disabled_tools: frozenset[str] = field(default_factory=frozenset)
    max_bulk_results: int = 100


def load_config() -> YouTrackConfig:
    url = os.environ.get("YOUTRACK_URL", "").rstrip("/")

    # Validate URL scheme
    if url and not url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
        import sys
        print(
            f"WARNING: YOUTRACK_URL ({url}) does not use HTTPS. "
            "Set YOUTRACK_ALLOW_HTTP=1 to allow insecure connections.",
            file=sys.stderr,
        )
        if not os.environ.get("YOUTRACK_ALLOW_HTTP"):
            url = ""

    # Parse disabled tools (comma-separated, case-insensitive)
    disabled_raw = os.environ.get("DISABLED_TOOLS", "")
    disabled = frozenset(
        t.strip().lower().replace("-", "_")
        for t in disabled_raw.split(",")
        if t.strip()
    )

    read_only = os.environ.get("YOUTRACK_READ_ONLY", "").lower() in ("1", "true", "yes")

    max_bulk = int(os.environ.get("YOUTRACK_MAX_BULK_RESULTS", "100"))

    return YouTrackConfig(
        url=url,
        token=os.environ.get("YOUTRACK_TOKEN", ""),
        read_only=read_only,
        disabled_tools=disabled,
        max_bulk_results=max_bulk,
    )
