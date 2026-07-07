"""Tests for scripts/sync-mcp-label.py pure logic.

The script lives in scripts/ with a hyphenated name, so it's loaded from
its file path. The version lookup is dependency-injected, so these tests
never spawn a subprocess or touch ~/.claude.json. Ported from the fleet
pattern (zbbx-mcp ADR 061/062).
"""

import importlib.util
import pathlib

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "sync-mcp-label.py"

# The real yt-mcp registration shape: uvx --from git+<url> yt-mcp
_YT_ARGS = ["--from", "git+https://github.com/velesnitski/yt-mcp", "yt-mcp"]


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("sync_mcp_label", _SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TestConstants:
    def test_display_and_match(self, mod):
        assert mod.DISPLAY == "youtrack"
        assert mod.BINARY_MATCH == "yt-mcp"


class TestParseSemver:
    def test_bare_version(self, mod):
        assert mod.parse_semver("1.15.0") == "1.15.0"

    def test_extracts_from_noisy_output(self, mod):
        # The server prints a startup log line to stderr; if any leaks to
        # stdout we still pull the version token.
        assert mod.parse_semver("Resolved 47 packages\n1.15.0\n") == "1.15.0"

    def test_keeps_prerelease_suffix(self, mod):
        assert mod.parse_semver("0.0.0+unknown") == "0.0.0+unknown"

    def test_no_version(self, mod):
        assert mod.parse_semver("no numbers here") == ""


class TestEntryMatches:
    def test_fragment_in_uvx_git_args(self, mod):
        # The actual yt-mcp registration: command=uvx, args carry the fragment.
        assert mod.entry_matches({"command": "uvx", "args": _YT_ARGS}) is True

    def test_fragment_in_uv_run_directory(self, mod):
        # Alternative local-dev registration.
        assert mod.entry_matches(
            {"command": "uv", "args": ["run", "--directory", "/x/yt-mcp", "yt-mcp"]}
        ) is True

    def test_fragment_in_command(self, mod):
        assert mod.entry_matches({"command": "/opt/yt-mcp/bin/yt-mcp", "args": []}) is True

    def test_unrelated_entry(self, mod):
        assert mod.entry_matches({"command": "uv", "args": ["run", "slk-mcp"]}) is False

    def test_non_dict(self, mod):
        assert mod.entry_matches("nope") is False


class TestWiredDirectory:
    def test_extracts_directory(self, mod):
        assert mod._wired_directory(["run", "--directory", "/x/yt-mcp", "yt-mcp"]) == "/x/yt-mcp"

    def test_absent_for_uvx_git(self, mod):
        # uvx --from git+... has no --directory.
        assert mod._wired_directory(_YT_ARGS) == ""

    def test_dangling_flag(self, mod):
        assert mod._wired_directory(["run", "--directory"]) == ""


class TestVersionFromPyproject:
    def test_reads_project_version(self, mod, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "yt-mcp"\nversion = "1.15.0"\n'
        )
        assert mod.version_from_pyproject(str(tmp_path)) == "1.15.0"

    def test_missing_file(self, mod, tmp_path):
        assert mod.version_from_pyproject(str(tmp_path)) == ""


class TestMcpContainers:
    def test_root_and_projects(self, mod):
        cfg = {
            "mcpServers": {"a": {}},
            "projects": {"/p1": {"mcpServers": {"b": {}}}, "/p2": {"other": 1}},
        }
        conts = mod.mcp_containers(cfg)
        assert len(conts) == 2  # root + p1; p2 has no mcpServers


class TestRenameIn:
    def _container(self):
        return {
            "youtrack": {"command": "uvx", "args": list(_YT_ARGS)},
            "slack": {"command": "uv", "args": ["run", "slk-mcp"]},
        }

    def test_renames_to_versioned_key(self, mod):
        c = self._container()
        changed = mod.rename_in(c, get_version=lambda cmd, args: "1.15.0")
        assert changed is True
        assert "youtrack v1.15.0" in c
        assert "youtrack" not in c
        assert "slack" in c  # untouched

    def test_preserves_insertion_order(self, mod):
        c = self._container()
        mod.rename_in(c, get_version=lambda cmd, args: "1.15.0")
        assert list(c.keys()) == ["youtrack v1.15.0", "slack"]

    def test_renames_already_versioned_key_on_bump(self, mod):
        # Entry found by path fragment, not key — so a stale versioned key
        # is re-keyed to the new version.
        c = {"youtrack v1.14.1": {"command": "uvx", "args": list(_YT_ARGS)}}
        assert mod.rename_in(c, get_version=lambda cmd, args: "1.15.0") is True
        assert "youtrack v1.15.0" in c
        assert "youtrack v1.14.1" not in c

    def test_idempotent_when_already_current(self, mod):
        c = {"youtrack v1.15.0": {"command": "uvx", "args": list(_YT_ARGS)}}
        assert mod.rename_in(c, get_version=lambda cmd, args: "1.15.0") is False

    def test_skips_when_no_version(self, mod):
        c = self._container()
        assert mod.rename_in(c, get_version=lambda cmd, args: "") is False
        assert "youtrack" in c  # unchanged


class TestSyncConfig:
    """sync_config must update EVERY container, not short-circuit (zbbx ADR 062)."""

    def test_renames_across_all_containers(self, mod):
        yt = {"command": "uvx", "args": list(_YT_ARGS)}
        cfg = {
            "projects": {
                "/p1": {"mcpServers": {"youtrack": dict(yt)}},
                "/p2": {"mcpServers": {"youtrack": dict(yt)}},
            }
        }
        changed = mod.sync_config(cfg, get_version=lambda cmd, args: "1.15.1")
        assert changed is True
        # Both project containers must be re-keyed — the short-circuit bug
        # left the second as plain "youtrack".
        keys = [list(p["mcpServers"].keys())[0] for p in cfg["projects"].values()]
        assert keys == ["youtrack v1.15.1", "youtrack v1.15.1"]

    def test_returns_false_when_nothing_matches(self, mod):
        cfg = {"mcpServers": {"slack": {"command": "uv", "args": ["run", "slk-mcp"]}}}
        assert mod.sync_config(cfg, get_version=lambda cmd, args: "1.15.1") is False


class TestPinArgs:
    """--pin rewrites the --from git spec to the released tag (ADR-025)."""

    def test_pins_unpinned_spec(self, mod):
        args, changed = mod.pin_args(_YT_ARGS, "v1.17.4")
        assert changed
        assert "git+https://github.com/velesnitski/yt-mcp@v1.17.4" in args
        assert args[0] == "--from" and args[-1] == "yt-mcp"

    def test_repins_old_ref(self, mod):
        old = ["--from", "git+https://github.com/velesnitski/yt-mcp@v1.17.3", "yt-mcp"]
        args, changed = mod.pin_args(old, "v1.17.4")
        assert changed
        assert "git+https://github.com/velesnitski/yt-mcp@v1.17.4" in args
        assert not any("@v1.17.3" in a for a in args)

    def test_idempotent_when_already_pinned(self, mod):
        pinned = ["--from", "git+https://github.com/velesnitski/yt-mcp@v1.17.4", "yt-mcp"]
        args, changed = mod.pin_args(pinned, "v1.17.4")
        assert not changed
        assert args == pinned

    def test_other_args_untouched(self, mod):
        args, changed = mod.pin_args(["--quiet", "yt-mcp"], "v1")
        assert not changed and args == ["--quiet", "yt-mcp"]

    def test_rename_in_pins_before_version_query(self, mod):
        # The version query must run against the PINNED args, so the label
        # reflects the release just pinned — not whatever the cache held.
        container = {"youtrack v1.17.3": {
            "command": "uvx", "args": list(_YT_ARGS), "type": "stdio"}}
        seen_args = []
        def fake_version(command, args):
            seen_args.append(list(args))
            return "1.17.4"
        changed = mod.rename_in(container, fake_version, pin="v1.17.4")
        assert changed
        assert "youtrack v1.17.4" in container
        assert any("@v1.17.4" in a for a in seen_args[0])
