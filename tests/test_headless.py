"""Headless mode: `yt-mcp run <tool>` / `yt-mcp tools` (ADR-065).

Pins what a scheduler relies on: the exit code says what went wrong, stdout
carries only the result, arguments are typed and checked against the tool's
own schema, and the read-only / disabled gating is the server's, not a copy.
Network is never touched: YouTrack replies are served by a patched transport.
"""

import asyncio
import io
import json

import httpx
import pytest

from yt_mcp import client as client_mod
from yt_mcp.headless import (
    EXIT_OK, EXIT_TOOL_ERROR, EXIT_USAGE, _duration, UsageError, is_headless, run_headless,
)


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    for var in ("YOUTRACK_INSTANCES", "YOUTRACK_READ_ONLY", "DISABLED_TOOLS",
                "YOUTRACK_TOOLSET", "YOUTRACK_LOG_LEVEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("YOUTRACK_URL", "https://example.youtrack.cloud")
    monkeypatch.setenv("YOUTRACK_TOKEN", "headless-fake")
    monkeypatch.setenv("YOUTRACK_LOG_FILE", str(tmp_path / "l.log"))
    monkeypatch.setenv("YOUTRACK_ANALYTICS_FILE", str(tmp_path / "a.log"))


@pytest.fixture
def youtrack(monkeypatch):
    """Serve every YouTrack request from `reply(request) -> httpx.Response`."""
    state = {"reply": lambda req: httpx.Response(200, json=[]), "requests": []}

    async def fake_send(self, method, path, **kwargs):
        req = httpx.Request(method, "https://example.youtrack.cloud" + path,
                            params=kwargs.get("params"))
        state["requests"].append(req)
        out = state["reply"](req)
        if asyncio.iscoroutine(out):
            out = await out
        out.request = req
        return out

    monkeypatch.setattr(client_mod.YouTrackClient, "_send", fake_send)
    return state


def cli(*args, stdin=""):
    out, err = io.StringIO(), io.StringIO()
    code = run_headless(list(args), io.StringIO(stdin), out, err)
    return code, out.getvalue(), err.getvalue()


def test_dispatch_only_on_subcommands():
    assert is_headless(["run", "x"]) and is_headless(["tools"])
    assert not is_headless([]) and not is_headless(["--transport", "stdio"])
    assert not is_headless(["--version"])


class TestExitCodes:
    @pytest.mark.parametrize("args,needle", [
        (["run"], "usage:"),
        (["run", "--json"], "usage:"),
        (["run", "nope"], "unknown tool 'nope'"),
        (["run", "get_issue"], "get_issue needs issue_id="),
        (["run", "get_issue", "issue_id=A-1", "bogus=1"], "takes: comment_chars, fields"),
        (["run", "get_issue", "issue_id"], "is not key=value"),
        (["run", "search_issues", "query=x", "max_results=abc"], "not a whole number"),
        (["run", "get_issue", "issue_id=A-1", "include_comments=maybe"], "not true or false"),
        (["run", "get_issue", "issue_id=A-1", "--timeout", "soon"], "not a duration"),
        (["run", "get_issue", "issue_id=A-1", "--timeout"], "--timeout needs a value"),
        (["run", "get_issue", "--args", "[1]"], "must be a JSON object"),
        (["run", "get_issue", "--args", "{nope"], "--args is not JSON"),
        (["run", "get_issue", "--args", '{"bogus": 1, "issue_id": "A-1"}'], "takes no argument 'bogus'"),
        (["run", "get_issue", "--frobnicate"], "unknown flag"),
        (["tools", "--yaml"], "usage: yt-mcp tools"),
    ])
    def test_usage_errors_exit_2_with_reason_on_stderr(self, args, needle, youtrack):
        code, out, err = cli(*args)
        assert code == EXIT_USAGE
        assert needle in err and out == ""
        assert youtrack["requests"] == []  # refused before any call

    def test_missing_token_is_a_configuration_error(self, monkeypatch):
        monkeypatch.setenv("YOUTRACK_TOKEN", "")
        code, _, err = cli("tools")
        assert code == EXIT_USAGE and "no YouTrack token" in err
        assert "headless-fake" not in err

    def test_command_line_is_checked_before_the_server_is_built(self, monkeypatch):
        # The cheaper mistake is reported first, without a server start.
        monkeypatch.setenv("YOUTRACK_TOKEN", "")
        code, _, err = cli("run", "get_issue", "--frobnicate")
        assert code == EXIT_USAGE and "unknown flag" in err

    def test_tool_error_exits_1(self, youtrack):
        youtrack["reply"] = lambda req: httpx.Response(404, json={"error": "nope"})
        code, out, err = cli("run", "get_issue", "issue_id=A-1")
        assert code == EXIT_TOOL_ERROR and out == ""
        assert "not found" in err

    def test_timeout_exits_1(self, youtrack):
        async def slow(req):
            await asyncio.sleep(5)
            return httpx.Response(200, json={})
        youtrack["reply"] = slow
        code, out, _ = cli("run", "get_issue", "issue_id=A-1", "--timeout", "50ms", "--json")
        assert code == EXIT_TOOL_ERROR
        assert json.loads(out) == {"tool": "get_issue", "args": {"issue_id": "A-1"},
                                   "ok": False, "text": "get_issue timed out after 0.05s"}


class TestRun:
    def test_success_prints_only_the_result(self, youtrack, capsys):
        code, out, err = cli("run", "list_templates")
        assert code == EXIT_OK
        assert out.startswith("## Available issue templates")
        # The log handler writes to the PROCESS stderr, not the buffer cli()
        # passes, so check that too: INFO startup lines must stay off it.
        assert err == "" and capsys.readouterr().err == ""

    def test_log_level_can_be_raised_for_debugging(self, youtrack, capsys, monkeypatch):
        monkeypatch.setenv("YOUTRACK_LOG_LEVEL", "info")
        cli("run", "list_templates")
        assert "Starting yt-mcp" in capsys.readouterr().err

    def test_json_shape(self, youtrack):
        code, out, _ = cli("run", "list_templates", "--json")
        doc = json.loads(out)
        assert code == EXIT_OK and set(doc) == {"tool", "args", "ok", "text"}
        assert doc["ok"] is True and doc["text"].startswith("## Available")

    def test_pairs_are_typed_by_schema_and_flags_interleave(self, youtrack):
        code, out, _ = cli("run", "search_issues", "--json", "query=project: DEMO",
                           "--timeout=30s", "max_results=7")
        assert code == EXIT_OK
        assert json.loads(out)["args"] == {"query": "project: DEMO", "max_results": 7}
        assert youtrack["requests"][0].url.params["$top"] == "7"

    def test_args_from_stdin_with_pair_override(self, youtrack):
        code, out, _ = cli("run", "search_issues", "--args", "-", "max_results=3", "--json",
                           stdin='{"query": "#Unresolved", "max_results": 50}')
        assert code == EXIT_OK
        assert json.loads(out)["args"] == {"query": "#Unresolved", "max_results": 3}

    def test_value_may_contain_equals_and_dashes(self, youtrack):
        code, out, _ = cli("run", "search_issues", "query=summary: a=b --x", "--json")
        assert json.loads(out)["args"]["query"] == "summary: a=b --x"

    def test_goes_through_the_same_wrappers_as_a_client(self, youtrack, tmp_path):
        # The analytics line proves the call took the registered, wrapped
        # path — not a private shortcut to the function.
        cli("run", "list_templates")
        line = json.loads((tmp_path / "a.log").read_text().splitlines()[-1])
        assert line["tool"] == "list_templates" and line["status"] == "ok"


class TestGating:
    def test_read_only_hides_write_tools(self, monkeypatch, youtrack):
        monkeypatch.setenv("YOUTRACK_READ_ONLY", "1")
        code, _, err = cli("run", "delete_issue", "issue_id=A-1")
        assert code == EXIT_USAGE and "unknown tool 'delete_issue'" in err
        _, listing, _ = cli("tools")
        assert "delete_issue" not in listing and "get_issue" in listing

    def test_disabled_tools_are_gone(self, monkeypatch, youtrack):
        monkeypatch.setenv("DISABLED_TOOLS", "get_issue")
        code, _, _ = cli("run", "get_issue", "issue_id=A-1")
        assert code == EXIT_USAGE


class TestTools:
    def test_listing_is_sorted_one_per_line(self, youtrack):
        code, out, _ = cli("tools")
        names = [line.split()[0] for line in out.splitlines()]
        assert code == EXIT_OK and names == sorted(names) and "get_issue" in names

    def test_json_carries_input_schemas(self, youtrack):
        code, out, _ = cli("tools", "--json")
        defs = {d["name"]: d for d in json.loads(out)}
        assert code == EXIT_OK
        assert "issue_id" in defs["get_issue"]["inputSchema"]["properties"]


def test_readme_examples_name_real_arguments(youtrack):
    """Every `yt-mcp run <tool> k=v` line in the README's headless section
    must pass the same argument check a user's command would."""
    import pathlib
    import re
    import shlex
    readme = (pathlib.Path(__file__).resolve().parents[1] / "README.md").read_text()
    section = readme.split("## Headless mode", 1)[1].split("\n## ", 1)[0]
    _, listing, _ = cli("tools", "--json")
    defs = {d["name"]: d for d in json.loads(listing)}
    examples = re.findall(r"yt-mcp run (\w+)([^\n]*)", section)
    assert examples
    for tool, rest in examples:
        if tool not in defs:  # e.g. the uvx prefix line's placeholder
            continue
        props = defs[tool]["inputSchema"]["properties"]
        for token in shlex.split(rest.split("|")[0], comments=True):
            if "=" in token and not token.startswith("-"):
                assert token.split("=", 1)[0] in props, f"README: {tool} has no {token}"
    piped = re.findall(r"echo '(\{.*?\})' \| yt-mcp run (\w+)", section)
    for body, tool in piped:
        assert set(json.loads(body)) <= set(defs[tool]["inputSchema"]["properties"]), tool


@pytest.mark.parametrize("text,seconds", [
    ("90", 90), ("90s", 90), ("5m", 300), ("1h", 3600), ("500ms", 0.5), ("1.5m", 90),
])
def test_duration(text, seconds):
    assert _duration(text) == seconds


@pytest.mark.parametrize("text", ["0", "-1", "5d", "", "m"])
def test_bad_duration(text):
    with pytest.raises(UsageError):
        _duration(text)


# ---------------------------------------------------------------- eval (ADR-071)


class TestEval:
    def _write(self, tmp_path, cases):
        p = tmp_path / "cases.json"
        p.write_text(json.dumps({"cases": cases}))
        return str(p)

    @pytest.mark.parametrize("args,needle", [
        (["eval"], "usage: yt-mcp eval"),
        (["eval", "/nonexistent/cases.json"], "cannot read"),
        (["eval", "a.json", "b.json"], "unexpected argument"),
    ])
    def test_usage_errors(self, args, needle):
        code, out, err = cli(*args)
        assert code == EXIT_USAGE and needle in err

    def test_bad_case_file_is_a_usage_error(self, tmp_path):
        bad = tmp_path / "c.json"
        bad.write_text('{"cases": [{"name": "x", "tool": "get_issues"}]}')
        code, _, err = cli("eval", str(bad))
        assert code == EXIT_USAGE and "missing args, expect" in err

    def test_runs_cases_in_process_and_reports(self, tmp_path, youtrack):
        def reply(req):
            if req.url.path == "/api/issues":
                return httpx.Response(200, json=[{"idReadable": "DEMO-1", "summary": "x"}])
            return httpx.Response(404, json={"error": "nope"})
        youtrack["reply"] = reply
        path = self._write(tmp_path, [
            {"name": "batch ok", "tool": "get_issues", "args": {"ids": "DEMO-1,DEMO-2"},
             "expect": {"contains": ["DEMO-1"], "missing": ["DEMO-2"]}},
            {"name": "batch wrong", "tool": "get_issues", "args": {"ids": "DEMO-1"},
             "expect": {"contains": ["DEMO-7"]}},
        ])
        code, out, _ = cli("eval", path, "--json")
        doc = json.loads(out)
        assert code == 1 and (doc["passed"], doc["total"]) == (1, 2)
        assert doc["cases"][1]["problems"] == ["DEMO-7 not fetched"]


class TestJudge:
    def test_verdict_top_and_trap(self):
        from yt_mcp.evalcases import judge
        case = {"tool": "resolve_ticket_refs",
                "expect": {"id": "A-1", "verdict": ["confident"], "never": "B-1"}}
        assert judge(case, [{"verdict": "confident", "top": "A-1"}]) == []
        assert judge(case, [{"verdict": "weak", "top": "B-1"}]) == [
            "verdict weak not in ['confident']", "top B-1 != A-1", "linked the trap B-1"]
        assert judge({"tool": "find_issues_for_text", "expect": {"verdict": ["none"]}},
                     [{"verdict": "none", "top": None}]) == []
