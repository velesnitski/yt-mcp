"""One version, everywhere it is written (ADR-063).

The server reports `__version__` — to `--version` and in the instructions
header every client sees. Two releases bumped `pyproject.toml` and the lock
but not `__init__.py`, so a 1.34.3 build announced itself as 1.34.1. Nothing
failed: the release script that bumps both was bypassed, and no test
compared them. This one does, so CI catches a skew however a release is cut.

Parsed with regexes rather than tomllib, which is missing on Python 3.10 —
the floor of the test matrix.
"""

import pathlib
import re

import yt_mcp

REPO = pathlib.Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    text = (REPO / "pyproject.toml").read_text()
    project = text.split("[project]", 1)[1].split("\n[", 1)[0]
    m = re.search(r'^version\s*=\s*"([^"]+)"', project, re.M)
    assert m, "no version in [project]"
    return m.group(1)


def _lock_version() -> str:
    text = (REPO / "uv.lock").read_text()
    m = re.search(r'\[\[package\]\]\s*\nname = "yt-mcp"\s*\nversion = "([^"]+)"', text)
    assert m, "yt-mcp entry not found in uv.lock"
    return m.group(1)


def test_package_reports_the_pyproject_version():
    assert yt_mcp.__version__ == _pyproject_version(), (
        "__init__.py and pyproject.toml disagree — the server would announce "
        "a version it is not"
    )


def test_lockfile_matches_pyproject():
    assert _lock_version() == _pyproject_version()


def test_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", _pyproject_version())
