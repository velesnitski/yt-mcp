#!/usr/bin/env python3
"""Classify a pull request by risk, and refuse banned strings (ADR-060).

Runs from `.github/workflows/semantic-gate.yml` under `pull_request_target`,
so it always executes from the BASE branch: a pull request cannot edit the
code that judges it. It never runs anything from the pull request — the
changed files and their patches arrive as JSON fetched from the API, and are
treated as data.

What it decides, and what it does not:

* ``block`` — an added line, the title or the body matches a banned pattern.
  The only level that fails the check.
* ``high`` / ``medium`` / ``low`` — labels that tell the reviewer where to
  look harder. They grant nothing: review stays GitHub's native, required
  review. Nothing here can approve or merge.

Fail-closed by construction: a path that matches no rule takes the policy's
``default`` level, a malformed policy or an invalid pattern is an error, and
missing patterns are an error rather than a silent pass.

The pattern list is secret and the logs are public, so neither patterns nor
matching text are ever printed — only a count.

Stdlib only, so it needs no install step and runs on every Python the test
matrix covers.
"""

import argparse
import base64
import json
import os
import re
import sys
from pathlib import Path

LEVELS = ("low", "medium", "high", "block")
_RANK = {lvl: i for i, lvl in enumerate(LEVELS)}
_PATH_LEVELS = ("high", "medium", "low")

# Never swept: it holds placeholder patterns by design.
_SWEEP_EXCLUDE = {".sweep-patterns.example"}

# Bots whose pull requests run without repository secrets. Their changes are
# dependency bumps, which cannot carry our banned strings.
_SECRETLESS_BOTS = {"dependabot[bot]"}


class PolicyError(ValueError):
    """The policy or the pattern list is unusable; the check must fail."""


def glob_to_regex(glob: str) -> re.Pattern:
    """Translate a path glob into an anchored regex.

    `**/` spans zero or more directories, `**` spans anything, `*` and `?`
    stay within one path segment, and every other character is literal.
    Tokenised rather than chained substitutions — the bash version needed
    placeholder tokens and still got `**` wrong once.
    """
    out, i = [], 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    # DOTALL and \Z, not `$`: a filename may contain a newline, and with the
    # defaults `.*` stops at it and `$` matches before a trailing one — so
    # `.github/x\n…` escaped `.github/**` and came out under the default.
    return re.compile("^" + "".join(out) + r"\Z", re.DOTALL)


def load_policy(path: Path) -> dict:
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise PolicyError(f"cannot read policy {path}: {e}") from None
    default = raw.get("default")
    if default not in _PATH_LEVELS:
        raise PolicyError(f"policy default must be one of {_PATH_LEVELS}, got {default!r}")
    compiled = {}
    for level in _PATH_LEVELS:
        globs = raw.get(level, [])
        if not isinstance(globs, list) or not all(isinstance(g, str) and g for g in globs):
            raise PolicyError(f"policy {level!r} must be a list of non-empty globs")
        compiled[level] = [(g, glob_to_regex(g)) for g in globs]
    unknown = set(raw) - set(_PATH_LEVELS) - {"default", "_comment"}
    if unknown:
        raise PolicyError(f"unknown policy keys: {sorted(unknown)}")
    return {"default": default, "rules": compiled}


def path_level(path: str, policy: dict) -> str:
    """Highest level any rule assigns to `path`; the default if none match.

    Highest, not first: README.md matches both a high rule and `**/*.md`
    (low), and must come out high. Rules can only ever raise a level.
    """
    best = None
    for level in _PATH_LEVELS:
        if any(rx.match(path) for _, rx in policy["rules"][level]):
            if best is None or _RANK[level] > _RANK[best]:
                best = level
    return best or policy["default"]


def classify_paths(paths: list[str], policy: dict) -> tuple[str, dict[str, list[str]]]:
    by_level: dict[str, list[str]] = {}
    for p in paths:
        by_level.setdefault(path_level(p, policy), []).append(p)
    if not by_level:
        return "low", {}
    overall = max(by_level, key=lambda lvl: _RANK[lvl])
    return overall, by_level


def load_patterns(b64: str) -> list[re.Pattern]:
    """Decode the sweep list: one regex per line, `CS:` = case-sensitive.

    Same format as scripts/sweep.sh. An invalid pattern is an error, not a
    skip — skipping it would quietly stop guarding whatever it covered.
    """
    try:
        text = base64.b64decode(b64, validate=False).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as e:
        raise PolicyError(f"sweep patterns are not valid base64/utf-8: {e}") from None
    patterns = []
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        flags = 0
        if line.startswith("CS:"):
            line = line[3:]
        else:
            flags = re.IGNORECASE
        try:
            patterns.append(re.compile(line, flags))
        except re.error:
            # The line number is safe to print; the pattern is not.
            raise PolicyError(f"sweep pattern on line {n} is not a valid regex") from None
    if not patterns:
        raise PolicyError("sweep pattern list is empty")
    return patterns


def added_lines(files: list[dict]) -> tuple[list[str], list[str]]:
    """Added lines from every patch, plus the files that have no patch.

    The API omits `patch` for binary files and very large diffs. Those
    cannot be swept, which is worth saying rather than passing over.
    """
    lines, unscannable = [], []
    for f in files:
        name = f.get("filename") or ""
        if name in _SWEEP_EXCLUDE or f.get("status") == "removed":
            continue
        patch = f.get("patch")
        if patch is None:
            unscannable.append(name)
            continue
        for line in patch.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                lines.append(line[1:])
    return lines, unscannable


def count_matches(texts: list[str], patterns: list[re.Pattern]) -> int:
    return sum(1 for t in texts if any(p.search(t) for p in patterns))


def _flatten(files) -> list[dict]:
    # `gh api --paginate --slurp` yields a list of pages; tolerate either shape.
    if files and isinstance(files[0], list):
        return [f for page in files for f in page]
    return files


def _md(path: str) -> str:
    """A filename is attacker-controlled; keep it inert in markdown."""
    clean = path.replace("`", "'").replace("\n", " ").replace("\r", " ")
    return f"`{clean[:200]}`"


def run(files: list[dict], meta: dict, policy: dict, patterns_b64: str, author: str) -> tuple[str, list[str]]:
    """Return (risk, summary lines). Raises PolicyError on misconfiguration."""
    files = _flatten(files)
    paths = [f.get("filename") or "" for f in files]
    risk, by_level = classify_paths(paths, policy)

    summary = ["## Semantic gate", ""]
    for level in ("high", "medium", "low"):
        hits = by_level.get(level, [])
        if hits:
            shown = ", ".join(_md(p) for p in hits[:15])
            more = f" (+{len(hits) - 15} more)" if len(hits) > 15 else ""
            summary.append(f"- **{level}** ({len(hits)}): {shown}{more}")

    if patterns_b64:
        patterns = load_patterns(patterns_b64)
        lines, unscannable = added_lines(files)
        texts = lines + [meta.get("title") or "", meta.get("body") or ""]
        n = count_matches(texts, patterns)
        if n:
            risk = "block"
            summary.append(f"- **block**: {n} added line(s) or PR text match banned patterns "
                           "(not shown — the pattern list is private and these logs are public)")
        if unscannable:
            if _RANK[risk] < _RANK["high"]:
                risk = "high"
            summary.append(f"- {len(unscannable)} file(s) have no diff to sweep (binary or too large): "
                           + ", ".join(_md(p) for p in unscannable[:10]))
    elif author in _SECRETLESS_BOTS:
        summary.append(f"- sweep skipped: {_md(author)} runs without secrets (dependency bump)")
    else:
        raise PolicyError(
            "SWEEP_PATTERNS_B64 is not set — refusing to pass a check that cannot sweep"
        )

    summary.insert(1, f"**Risk: {risk}** — labels guide review; native required review still decides.")
    return risk, summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--policy", required=True)
    ap.add_argument("--files", required=True, help="JSON from GET /pulls/N/files")
    ap.add_argument("--meta", required=True, help="JSON with title and body")
    ap.add_argument("--author", default="")
    # The name of the variable holding the pattern list — never the value.
    # Arguments are visible to every process on the runner; the environment
    # of one step is not.
    ap.add_argument("--patterns-env", default="SWEEP_PATTERNS_B64")
    ap.add_argument("--out", help="file to append `risk=<level>` to (GITHUB_OUTPUT)")
    ap.add_argument("--summary", help="file to append the markdown summary to")
    args = ap.parse_args(argv)

    try:
        policy = load_policy(Path(args.policy))
        files = json.loads(Path(args.files).read_text())
        meta = json.loads(Path(args.meta).read_text())
        risk, summary = run(files, meta, policy, os.environ.get(args.patterns_env, ""), args.author)
    except PolicyError as e:
        print(f"::error::semantic-gate: {e}")
        return 2

    if args.out:
        # Only the computed level is ever written here. A filename containing a
        # newline must not be able to inject a second `risk=` line.
        with open(args.out, "a") as fh:
            fh.write(f"risk={risk}\n")
    if args.summary:
        with open(args.summary, "a") as fh:
            fh.write("\n".join(summary) + "\n")
    print(f"risk={risk} files={len(_flatten(files))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
