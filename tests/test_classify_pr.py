"""PR risk classifier and its workflow (ADR-060).

The first draft of this gate passed its own syntax checks and failed its
threat model: the PR controlled the gate, an unmatched path defaulted to the
lowest level, a missing tool made everything pass, an indentation change
counted as "whitespace only", and any GitHub account could approve. Each of
those has a test here, plus static guards on the workflow, because the
safety of `pull_request_target` lives in a few lines that are easy to edit
away.
"""

import base64
import importlib.util
import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "semantic-gate.yml"
POLICY = REPO / ".github" / "risk-policy.json"


def _load():
    spec = importlib.util.spec_from_file_location("classify_pr", REPO / "scripts" / "classify_pr.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cp = _load()

# Made-up words standing in for the private list; nothing key-shaped.
_PATTERNS = "# comment\n\nforbiddenword\nCS:SHOUTCODE\n"


def _b64(text):
    return base64.b64encode(text.encode()).decode()


def _file(name, added=(), status="modified", patch=True):
    f = {"filename": name, "status": status}
    if patch:
        f["patch"] = "@@ -1 +1 @@\n" + "".join(f"+{line}\n" for line in added) + " context\n"
    return f


def _policy():
    return cp.load_policy(POLICY)


class TestGlob:
    @pytest.mark.parametrize("glob,path,ok", [
        (".github/**", ".github/workflows/x.yml", True),
        (".github/**", ".github/CODEOWNERS", True),
        (".github/**", "x/.github/y", False),
        ("src/**", "src/a.py", True),
        ("src/**", "src/a/b/c.py", True),
        ("**/*.md", "README.md", True),
        ("**/*.md", "docs/adr/001.md", True),
        ("**/*.md", "notes.md.txt", False),
        ("scripts/release.sh", "scripts/release.sh", True),
        ("scripts/release.sh", "scripts/release.sh.bak", False),
        ("uv.lock", "uvXlock", False),
        ("*.py", "a/b.py", False),
        ("docs/?.md", "docs/a.md", True),
        ("docs/?.md", "docs/ab.md", False),
        (".github/**", ".github/x\nrisk=low", True),   # newline must not escape a rule
        ("README.md", "README.md\n", False),           # nor a trailing one sneak a match
    ])
    def test_glob(self, glob, path, ok):
        assert bool(cp.glob_to_regex(glob).match(path)) is ok


class TestClassification:
    """A level can only be raised; an unmatched path is not trusted."""

    @pytest.mark.parametrize("path,level", [
        (".github/workflows/semantic-gate.yml", "high"),
        (".github/risk-policy.json", "high"),     # the policy cannot downgrade itself
        ("scripts/classify_pr.py", "high"),       # nor can the classifier
        ("CLAUDE.md", "high"),                    # read by every agent session
        ("README.md", "high"),                    # carries the install command
        ("SECURITY.md", "high"),
        (".gitignore", "high"),                   # keeps untracked notes out of commits
        ("src/yt_mcp/tools/__init__.py", "high"), # WRITE_TOOLS / read-only mode
        ("src/yt_mcp/logging.py", "high"),        # error-report scrubbing
        ("src/yt_mcp/contract.py", "medium"),
        ("tests/test_x.py", "medium"),
        ("docs/adr/001-x.md", "low"),
        ("LICENSE", "low"),
    ])
    def test_real_policy(self, path, level):
        assert cp.path_level(path, _policy()) == level

    def test_unmatched_path_takes_the_default_not_low(self):
        """The first draft let anything unlisted fall to low — fail-open."""
        assert cp.path_level("Makefile", _policy()) == "medium"
        assert cp.path_level(".python-version", _policy()) == "medium"

    def test_highest_rule_wins(self):
        risk, by_level = cp.classify_paths(["docs/a.md", "src/yt_mcp/auth.py"], _policy())
        assert risk == "high"
        assert "src/yt_mcp/auth.py" in by_level["high"]

    def test_highest_wins_regardless_of_rule_order(self, monkeypatch):
        """Rules are scanned high→medium→low, so "first match" would happen
        to equal "highest" — until someone reorders them. Pin the property,
        not the coincidence."""
        monkeypatch.setattr(cp, "_PATH_LEVELS", ("low", "medium", "high"))
        assert cp.path_level("README.md", _policy()) == "high"
        assert cp.path_level("src/yt_mcp/auth.py", _policy()) == "high"

    def test_no_files_is_low(self):
        assert cp.classify_paths([], _policy())[0] == "low"

    def test_no_downgrade_rules_exist(self):
        """Whitespace- or comment-only downgrades were removed: in Python
        indentation is syntax, and in this server docstrings are the tool
        descriptions the model reads."""
        raw = json.loads(POLICY.read_text())
        assert not {k for k in raw if "if" in k or "downgrade" in k}
        assert "ignore-all-space" not in WORKFLOW.read_text()


class TestPolicyValidation:
    def _write(self, tmp_path, data):
        p = tmp_path / "p.json"
        p.write_text(json.dumps(data))
        return p

    def test_missing_policy_is_an_error(self, tmp_path):
        with pytest.raises(cp.PolicyError):
            cp.load_policy(tmp_path / "nope.json")

    def test_malformed_json_is_an_error(self, tmp_path):
        p = tmp_path / "p.json"
        p.write_text("{not json")
        with pytest.raises(cp.PolicyError):
            cp.load_policy(p)

    def test_bad_default_is_an_error(self, tmp_path):
        with pytest.raises(cp.PolicyError):
            cp.load_policy(self._write(tmp_path, {"default": "trusted"}))

    def test_typo_in_a_key_is_an_error(self, tmp_path):
        """`hihg` silently ignored would drop every rule under it."""
        with pytest.raises(cp.PolicyError, match="unknown"):
            cp.load_policy(self._write(tmp_path, {"default": "medium", "hihg": ["a"]}))

    def test_non_list_rules_are_an_error(self, tmp_path):
        with pytest.raises(cp.PolicyError):
            cp.load_policy(self._write(tmp_path, {"default": "medium", "high": "src/**"}))


class TestSweep:
    def _run(self, files, meta=None, author="someone", patterns=_PATTERNS):
        return cp.run(files, meta or {}, _policy(), _b64(patterns) if patterns else "", author)

    def test_clean_diff_is_not_blocked(self):
        risk, _ = self._run([_file("docs/a.md", ["hello"])])
        assert risk == "low"

    def test_case_insensitive_pattern_blocks(self):
        risk, _ = self._run([_file("docs/a.md", ["a FORBIDDENWORD here"])])
        assert risk == "block"

    def test_cs_pattern_is_case_sensitive(self):
        assert self._run([_file("docs/a.md", ["SHOUTCODE"])])[0] == "block"
        assert self._run([_file("docs/a.md", ["shoutcode"])])[0] == "low"

    def test_only_added_lines_count(self):
        f = {"filename": "docs/a.md", "status": "modified",
             "patch": "@@ -1 +1 @@\n-forbiddenword\n context forbiddenword\n+fine\n"}
        assert self._run([f])[0] == "low"

    def test_diff_header_is_not_content(self):
        f = {"filename": "docs/a.md", "status": "modified",
             "patch": "+++ b/forbiddenword\n+fine\n"}
        assert self._run([f])[0] == "low"

    def test_pr_title_and_body_are_swept(self):
        """PR text is as public as the diff."""
        assert self._run([_file("docs/a.md", ["x"])], {"title": "fix forbiddenword"})[0] == "block"
        assert self._run([_file("docs/a.md", ["x"])], {"body": "see SHOUTCODE"})[0] == "block"

    def test_example_file_is_exempt(self):
        assert self._run([_file(".sweep-patterns.example", ["forbiddenword"])])[0] != "block"

    def test_removed_files_are_not_swept(self):
        assert self._run([_file("docs/a.md", ["forbiddenword"], status="removed")])[0] == "low"

    def test_unscannable_file_raises_to_high(self):
        risk, summary = self._run([_file("docs/big.md", patch=False)])
        assert risk == "high"
        assert any("no diff to sweep" in s for s in summary)

    def test_neither_patterns_nor_matches_are_ever_printed(self):
        """The pattern list is private and CI logs are public."""
        risk, summary = self._run([_file("docs/a.md", ["leak forbiddenword and SHOUTCODE"])],
                                  {"title": "forbiddenword"})
        text = "\n".join(summary).lower()
        assert risk == "block"
        assert "forbiddenword" not in text and "shoutcode" not in text

    def test_missing_patterns_fail_rather_than_pass(self):
        with pytest.raises(cp.PolicyError, match="not set"):
            self._run([_file("docs/a.md", ["x"])], patterns="")

    def test_dependency_bot_runs_without_patterns(self):
        """Dependabot PRs get no secrets; failing them all would be noise."""
        risk, summary = self._run([_file("uv.lock", ["v"])], author="dependabot[bot]", patterns="")
        assert risk == "high"
        assert any("sweep skipped" in s for s in summary)

    def test_invalid_pattern_is_an_error_and_not_echoed(self):
        with pytest.raises(cp.PolicyError) as exc:
            self._run([_file("docs/a.md", ["x"])], patterns="ok\n(unclosed\n")
        assert "(unclosed" not in str(exc.value)
        assert "line 2" in str(exc.value)

    def test_empty_pattern_list_is_an_error(self):
        with pytest.raises(cp.PolicyError):
            self._run([_file("docs/a.md", ["x"])], patterns="# only a comment\n")

    def test_paginated_pages_are_flattened(self):
        pages = [[_file("docs/a.md", ["x"])], [_file("src/yt_mcp/auth.py", ["y"])]]
        assert self._run(pages)[0] == "high"


class TestMainOutputs:
    def _main(self, tmp_path, files, monkeypatch, patterns=_PATTERNS):
        (tmp_path / "f.json").write_text(json.dumps(files))
        (tmp_path / "m.json").write_text(json.dumps({"title": "t", "body": ""}))
        out, summ = tmp_path / "out", tmp_path / "summary"
        monkeypatch.setenv("SWEEP_PATTERNS_B64", _b64(patterns) if patterns else "")
        rc = cp.main(["--policy", str(POLICY), "--files", str(tmp_path / "f.json"),
                      "--meta", str(tmp_path / "m.json"), "--author", "someone",
                      "--out", str(out), "--summary", str(summ)])
        return rc, out, summ

    def test_writes_exactly_one_risk_line(self, tmp_path, monkeypatch):
        rc, out, _ = self._main(tmp_path, [_file("src/yt_mcp/auth.py", ["x"])], monkeypatch)
        assert rc == 0
        assert out.read_text() == "risk=high\n"

    def test_a_filename_cannot_inject_an_output(self, tmp_path, monkeypatch):
        """A newline in a filename must not smuggle in `risk=low`."""
        evil = ".github/workflows/x.yml\nrisk=low"
        rc, out, summ = self._main(tmp_path, [_file(evil, ["x"])], monkeypatch)
        lines = out.read_text().splitlines()
        assert lines == ["risk=high"]
        assert "\nrisk=low" not in summ.read_text()

    def test_backticks_in_filenames_are_neutralised(self, tmp_path, monkeypatch):
        rc, _, summ = self._main(tmp_path, [_file("docs/a`b.md", ["x"])], monkeypatch)
        assert "a`b" not in summ.read_text()

    def test_config_error_exits_2(self, tmp_path, monkeypatch):
        rc, out, _ = self._main(tmp_path, [_file("docs/a.md", ["x"])], monkeypatch, patterns="")
        assert rc == 2
        assert not out.exists() or "risk=" not in out.read_text()


class TestWorkflowSafety:
    """`pull_request_target` is safe only under these conditions."""

    def _wf(self):
        return WORKFLOW.read_text()

    def test_runs_from_the_base_branch(self):
        wf = self._wf()
        assert re.search(r"^\s+pull_request_target:", wf, re.M)
        assert not re.search(r"^\s+pull_request:", wf, re.M), (
            "plain pull_request runs the PR's own copy of this workflow"
        )

    def test_never_checks_out_or_references_the_pr_head(self):
        wf = self._wf()
        assert "pull_request.head" not in wf
        assert "github.head_ref" not in wf
        assert "ref: ${{ github.event.pull_request.base.sha }}" in wf

    def test_checkout_does_not_keep_credentials(self):
        assert "persist-credentials: false" in self._wf()

    def test_actions_are_pinned_to_commits(self):
        for ref in re.findall(r"uses:\s*\S+@(\S+)", self._wf()):
            assert re.fullmatch(r"[0-9a-f]{40}", ref), f"unpinned action ref: {ref}"

    def test_no_pr_controlled_text_is_interpolated_into_run(self):
        """Title, body and branch names are attacker-controlled."""
        wf = self._wf()
        for field in ("pull_request.title", "pull_request.body", "head_ref",
                      "pull_request.head.ref", "pull_request.head.label"):
            assert field not in wf

    def test_secret_is_scoped_to_one_step_and_never_on_argv(self):
        wf = self._wf()
        assert wf.count("secrets.SWEEP_PATTERNS_B64") == 1
        assert "--patterns" not in wf.split("classify_pr.py", 1)[1].split("\n\n", 1)[0] or \
               "$SWEEP_PATTERNS_B64" not in wf

    def test_permissions_are_minimal(self):
        wf = self._wf()
        assert "contents: read" in wf
        assert "contents: write" not in wf
        assert "actions: write" not in wf

    def test_no_push_trigger_so_owner_pushes_are_untouched(self):
        assert not re.search(r"^\s+push:", self._wf(), re.M)

    def test_nothing_approves_or_merges(self):
        """The gate guides review; it must never act as the reviewer."""
        wf = self._wf()
        assert "gh pr merge" not in wf
        assert "--approve" not in wf
        assert "human-ok" not in wf
