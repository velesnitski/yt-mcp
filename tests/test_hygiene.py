"""Source hygiene ratchet (ADR-024).

Very long modules are where architecture goes to hide. The current largest
(monitoring.py ~1.2k lines) is still fine to navigate — for humans and for
AI tooling alike, what matters is function-level cohesion and greppable
names, not file count. So we do NOT split working files retroactively;
instead this cap forces the split conversation at the natural moment: when
someone is about to push a module past the ratchet.

If this test fails on your change, split along tool-family lines (the way
deadlines/ became a package) rather than raising the limit.
"""
import pathlib

MAX_LINES = 1400
SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "yt_mcp"


def test_no_module_exceeds_ratchet():
    offenders = {
        str(p.relative_to(SRC)): n
        for p in SRC.rglob("*.py")
        if (n := len(p.read_text().splitlines())) > MAX_LINES
    }
    assert not offenders, (
        f"Modules over {MAX_LINES} lines: {offenders} — split along "
        "tool-family lines (see docstring), don't raise the cap."
    )
