import os
from dataclasses import dataclass


@dataclass(frozen=True)
class YouTrackConfig:
    url: str
    token: str


def load_config() -> YouTrackConfig:
    return YouTrackConfig(
        url=os.environ.get("YOUTRACK_URL", "").rstrip("/"),
        token=os.environ.get("YOUTRACK_TOKEN", ""),
    )
