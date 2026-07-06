"""Regression guard: no tracked file may contain a banned string (ADR-023).

Runs scripts/sweep.sh against the repo's own tracked files. Skipped where
.sweep-patterns.local doesn't exist (CI, fresh clones) — the patterns are
deliberately untracked so they never leak; the guard bites on dev machines.
"""
import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.mark.skipif(
    not (ROOT / ".sweep-patterns.local").exists(),
    reason="no local sweep patterns (untracked by design)",
)
def test_tracked_files_are_sweep_clean():
    r = subprocess.run(
        [str(ROOT / "scripts" / "sweep.sh")],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert r.returncode == 0, f"sweep found banned strings:\n{r.stderr}"
