"""Headless mode: run any tool once from the command line (ADR-065).

    yt-mcp tools [--json]
    yt-mcp run <tool> [key=value ...] [--args JSON|-] [--json] [--timeout 5m]
    yt-mcp eval <cases.json> [--json] [--verbose] [--timeout 2m]   (ADR-071)

The command builds the server exactly as the transports do and calls it
through FastMCP's own `list_tools` / `call_tool`, so a scheduler — cron,
launchd, a CI job, another bot — gets exactly what an MCP client gets: the
same tools, the same YOUTRACK_READ_ONLY / DISABLED_TOOLS / YOUTRACK_TOOLSET
gating, the same output, the same analytics line. Nothing is wired per tool,
so a tool is reachable here the day it is registered. Same command shape and
exit codes as the Slack server's headless mode, so one scheduler drives both.

Exit codes: 0 success, 1 the tool reported an error (or timed out), 2 a
usage or configuration error. Logs go to stderr at WARNING by default
(YOUTRACK_LOG_LEVEL overrides), so stdout carries only the result.
"""

import asyncio
import json
import logging
import os
import re
import sys
from typing import Any, TextIO

EXIT_OK = 0
EXIT_TOOL_ERROR = 1
EXIT_USAGE = 2

_RUN_USAGE = (
    "usage: yt-mcp run <tool> [key=value ...] [--args JSON|-] [--json] [--timeout 5m]"
)
_DEFAULT_TIMEOUT = 300.0
_DURATION = re.compile(r"^(\d+(?:\.\d+)?)(ms|s|m|h)?$")
_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, None: 1.0}


class UsageError(Exception):
    """A mistake in the command line or configuration, not in YouTrack."""


def is_headless(argv: list[str]) -> bool:
    return bool(argv) and argv[0] in ("run", "tools", "eval")


def run_headless(argv: list[str], stdin: TextIO = sys.stdin,
                 stdout: TextIO = sys.stdout, stderr: TextIO = sys.stderr) -> int:
    """Run one headless command and return the process exit code."""
    try:
        return asyncio.run(_main(argv, stdin, stdout, stderr))
    except UsageError as e:
        print(f"yt-mcp: {e}", file=stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        return 130


async def _main(argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    command, rest = argv[0], argv[1:]
    # Parse before building: a usage mistake must not cost a server start.
    if command == "eval":
        return await _eval(rest, stdout)
    opts = _parse_run(rest) if command == "run" else _parse_tools(rest)
    bundle = _build()
    try:
        tools = sorted(await bundle.mcp.list_tools(), key=lambda t: t.name)
        if command == "tools":
            _print_tools(tools, opts["json"], stdout)
            return EXIT_OK
        return await _run(bundle.mcp, tools, opts, stdin, stdout, stderr)
    finally:
        await bundle.aclose()


async def _eval(args: list[str], stdout: TextIO) -> int:
    """Run a local case file against this checkout, one server start."""
    import time
    from yt_mcp import evalcases

    usage = "usage: yt-mcp eval <cases.json> [--json] [--verbose] [--timeout 2m]"
    opts = {"json": False, "verbose": False, "timeout": 120.0, "path": None}
    it = iter(args)
    for a in it:
        flag, eq, inline = a.partition("=")
        if a == "--json":
            opts["json"] = True
        elif a == "--verbose":
            opts["verbose"] = True
        elif flag == "--timeout":
            value = inline if eq else next(it, None)
            if value is None:
                raise UsageError("--timeout needs a value")
            opts["timeout"] = _duration(value)
        elif a.startswith("-") or opts["path"]:
            raise UsageError(f"unexpected argument {a!r}; {usage}")
        else:
            opts["path"] = a
    if not opts["path"]:
        raise UsageError(usage)
    try:
        cases = evalcases.load_cases(opts["path"])
    except ValueError as e:
        raise UsageError(str(e)) from None
    bundle = _build()
    try:
        t0 = time.monotonic()
        results = await evalcases.run_cases(bundle.mcp, cases, opts["timeout"])
        return evalcases.report(results, time.monotonic() - t0, stdout, opts["json"], opts["verbose"])
    finally:
        await bundle.aclose()


def _build():
    """The server, configured as for a client, with stderr kept quiet."""
    from yt_mcp.config import load_all_configs
    from yt_mcp.server import build_server

    # Refuse an unusable configuration up front, naming the variable: the
    # transports only discover a missing token on the first call.
    for name, cfg in load_all_configs().items():
        where = "" if name == "default" else f" for instance {name!r}"
        if not cfg.url:
            raise UsageError(
                f"no YouTrack URL{where} (set YOUTRACK_URL, or YOUTRACK_<NAME>_URL; "
                "http:// needs YOUTRACK_ALLOW_HTTP=1)")
        if not cfg.token:
            raise UsageError(f"no YouTrack token{where} (set YOUTRACK_TOKEN, or YOUTRACK_<NAME>_TOKEN)")

    level = os.environ.get("YOUTRACK_LOG_LEVEL", "WARNING").upper()
    return build_server(stderr_level=getattr(logging, level, logging.WARNING))


# ---------------------------------------------------------------- parsing


def _parse_tools(args: list[str]) -> dict:
    opts = {"json": False}
    for a in args:
        if a == "--json":
            opts["json"] = True
        else:
            raise UsageError(f"unknown argument {a!r}; usage: yt-mcp tools [--json]")
    return opts


def _parse_run(args: list[str]) -> dict:
    """Flags may sit anywhere among the key=value pairs, as people type them."""
    if not args or args[0].startswith("-"):
        raise UsageError(_RUN_USAGE)
    opts: dict[str, Any] = {"tool": args[0], "json": False, "args": None,
                            "timeout": _DEFAULT_TIMEOUT, "pairs": []}
    it = iter(args[1:])
    for a in it:
        flag, eq, inline = a.partition("=")
        if a == "--json":
            opts["json"] = True
        elif flag in ("--args", "--timeout"):
            value = inline if eq else next(it, None)
            if value is None:
                raise UsageError(f"{flag} needs a value")
            if flag == "--args":
                opts["args"] = value
            else:
                opts["timeout"] = _duration(value)
        elif a.startswith("-"):
            raise UsageError(f"unknown flag {a!r}; {_RUN_USAGE}")
        else:
            opts["pairs"].append(a)
    return opts


def _duration(text: str) -> float:
    """`90`, `90s`, `5m`, `1h`, `500ms` → seconds."""
    m = _DURATION.match(text.strip())
    if not m or float(m.group(1)) <= 0:
        raise UsageError(f"--timeout {text!r} is not a duration like 90s, 5m or 1h")
    return float(m.group(1)) * _UNIT_SECONDS[m.group(2)]


def _schema_type(schema: dict) -> str:
    """The declared JSON type, looking through Optional's anyOf/null."""
    if isinstance(schema.get("type"), str):
        return schema["type"]
    for alt in schema.get("anyOf") or []:
        t = alt.get("type")
        if isinstance(t, str) and t != "null":
            return t
    return "string"


_TRUE = {"true", "1", "yes", "y", "on"}
_FALSE = {"false", "0", "no", "n", "off"}


def _typed(key: str, raw: str, schema: dict) -> Any:
    kind = _schema_type(schema)
    if kind == "integer":
        try:
            return int(raw)
        except ValueError:
            raise UsageError(f"{key}={raw!r} is not a whole number") from None
    if kind == "number":
        try:
            return float(raw)
        except ValueError:
            raise UsageError(f"{key}={raw!r} is not a number") from None
    if kind == "boolean":
        low = raw.strip().lower()
        if low in _TRUE or low in _FALSE:
            return low in _TRUE
        raise UsageError(f"{key}={raw!r} is not true or false")
    if kind in ("array", "object"):
        try:
            return json.loads(raw)
        except ValueError as e:
            raise UsageError(f"{key} takes JSON: {e}") from None
    return raw


def build_arguments(tool, args_json: str | None, pairs: list[str], stdin: TextIO) -> dict:
    """Merge --args and key=value pairs, typed by the tool's input schema.

    A key the tool does not take is refused with the list of keys it does
    take, so a typo fails loudly instead of being silently ignored.
    """
    out: dict[str, Any] = {}
    if args_json is not None:
        raw = stdin.read() if args_json == "-" else args_json
        try:
            out = json.loads(raw)
        except ValueError as e:
            raise UsageError(f"--args is not JSON: {e}") from None
        if not isinstance(out, dict):
            raise UsageError("--args must be a JSON object")
    props: dict = (tool.inputSchema or {}).get("properties") or {}
    for p in pairs:
        key, eq, value = p.partition("=")
        if not eq or not key:
            raise UsageError(f"argument {p!r} is not key=value")
        if key not in props:
            raise UsageError(f"{tool.name} takes no argument {key!r}; it takes: {', '.join(sorted(props))}")
        out[key] = _typed(key, value, props[key])
    unknown = sorted(set(out) - set(props))
    if unknown:
        raise UsageError(f"{tool.name} takes no argument {unknown[0]!r}; it takes: {', '.join(sorted(props))}")
    missing = [r for r in (tool.inputSchema or {}).get("required") or [] if r not in out]
    if missing:
        raise UsageError(f"{tool.name} needs " + ", ".join(f"{m}=…" for m in missing))
    return out


# ---------------------------------------------------------------- commands


def _print_tools(tools, as_json: bool, stdout: TextIO) -> None:
    if as_json:
        defs = [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in tools]
        json.dump(defs, stdout, indent=2, ensure_ascii=False)
        stdout.write("\n")
        return
    for t in tools:
        first = (t.description or "").strip().split("\n", 1)[0]
        print(f"{t.name:30} {first}", file=stdout)


def _text_of(result: Any) -> str:
    """Join the text blocks of a call_tool result (content, or (content, structured))."""
    content = result[0] if isinstance(result, tuple) else result
    if isinstance(content, dict):  # structured-only result
        return json.dumps(content, ensure_ascii=False)
    return "\n".join(getattr(c, "text", "") for c in content or [] if getattr(c, "text", None) is not None)


async def _run(mcp, tools, opts: dict, stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    name = opts["tool"]
    tool = next((t for t in tools if t.name == name), None)
    if tool is None:
        raise UsageError(
            f"unknown tool {name!r} (it may be hidden by YOUTRACK_READ_ONLY, "
            "DISABLED_TOOLS or YOUTRACK_TOOLSET); `yt-mcp tools` lists what is available")
    arguments = build_arguments(tool, opts["args"], opts["pairs"], stdin)

    ok, text = True, ""
    try:
        result = await asyncio.wait_for(mcp.call_tool(name, arguments), opts["timeout"])
        text = _text_of(result)
    except asyncio.TimeoutError:
        ok, text = False, f"{name} timed out after {opts['timeout']:g}s"
    except Exception as e:  # FastMCP wraps every tool failure in ToolError
        ok, text = False, str(e)

    if opts["json"]:
        json.dump({"tool": name, "args": arguments, "ok": ok, "text": text},
                  stdout, ensure_ascii=False)
        stdout.write("\n")
    elif ok:
        print(text, file=stdout)
    else:
        print(f"yt-mcp: {text}", file=stderr)
    return EXIT_OK if ok else EXIT_TOOL_ERROR
