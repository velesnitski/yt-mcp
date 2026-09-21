"""The registry must not outrank the code (ADR-052).

Q17 was written down, cited from two ADRs, and violated twice. Q2 was
written down, cited, and wrong. Prose does not run, so these tests check
that each entry is anchored to something that exists — and that the
registry's own shape stays machine-readable, since every check here
depends on being able to parse it.
"""

import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
REGISTRY = REPO / "docs" / "YT_QUIRKS.md"

ROW = re.compile(r"^\|\s*(Q\d+)\s*\|(.+)$")


def _rows() -> list[tuple[str, list[str]]]:
    out = []
    for line in REGISTRY.read_text().splitlines():
        m = ROW.match(line)
        if m:
            cells = [c.strip() for c in m.group(2).split("|")]
            out.append((m.group(1), cells))
    return out


def _all_source() -> str:
    parts = []
    for sub in ("src", "tests", "scripts"):
        for p in (REPO / sub).rglob("*.py"):
            parts.append(p.read_text())
    return "\n".join(parts)


class TestRegistryIsParseable:
    def test_registry_exists_and_has_entries(self):
        assert REGISTRY.exists(), "the shared registry is missing"
        assert len(_rows()) >= 15, f"only {len(_rows())} entries parsed — table shape changed?"

    def test_every_row_has_all_columns(self):
        for qid, cells in _rows():
            assert len(cells) >= 4, f"{qid} has {len(cells)} columns, expected ID+4"

    def test_ids_are_unique_and_contiguous(self):
        ids = [q for q, _ in _rows()]
        assert len(ids) == len(set(ids)), f"duplicate IDs: {ids}"
        nums = sorted(int(q[1:]) for q in ids)
        assert nums == list(range(1, len(nums) + 1)), (
            f"IDs must stay contiguous so a retraction is visible as a rewritten "
            f"row rather than a gap: {nums}"
        )


class TestGuardsAreAnchored:
    """Each entry's guard column must point at something real."""

    def test_cited_adrs_exist(self):
        adr_dir = REPO / "docs" / "adr"
        existing = {p.name.split("-")[0] for p in adr_dir.glob("*.md")}
        missing = []
        for qid, cells in _rows():
            for num in re.findall(r"ADR[- ](\d{3})", " ".join(cells)):
                if num not in existing:
                    missing.append(f"{qid} cites ADR-{num}, which does not exist")
        assert not missing, "; ".join(missing)

    def test_guard_column_is_never_empty(self):
        for qid, cells in _rows():
            guard = cells[3] if len(cells) > 3 else ""
            assert guard.strip(), f"{qid} names no guard at all"

    def test_guard_names_code_or_says_it_does_not(self):
        """A guard is a test, a symbol, or an explicit admission of none.

        Q2's guard cited a code comment that did not exist. A guard that
        points only at prose is indistinguishable from no guard, so it has
        to say so in words a reader can trust.
        """
        source = _all_source()
        unanchored = []
        for qid, cells in _rows():
            guard = cells[3] if len(cells) > 3 else ""
            low = guard.lower()
            # A guard may legitimately live in the sibling repo, but it has
            # to say so — an unscoped symbol that exists nowhere here reads
            # as a guard in this repo and is not one.
            scoped_elsewhere = "reports-side only" in low or "no code path here" in low
            risk_accepted = (
                scoped_elsewhere
                or "registry entry only" in low
                or "registry entry is the guard" in low
                or "no code path" in low
            )
            if risk_accepted or "retest" in low or "retracted" in low:
                continue  # withdrawn, or documented as accepted risk
            # Symbols may carry call syntax (`added(text)`) or dotted paths.
            symbols = re.findall(r"`([A-Za-z_][A-Za-z0-9_.]{3,})", guard)
            cites_adr = bool(re.search(r"ADR[- ]\d{3}", guard))
            if symbols and any(sym in source for sym in symbols):
                continue
            if cites_adr:
                continue
            unanchored.append(f"{qid}: guard names nothing checkable ({guard[:60]}…)")
        assert not unanchored, "; ".join(unanchored)


class TestRetractionsStayVisible:
    def test_retracted_entries_keep_their_row(self):
        """A withdrawn quirk is rewritten, never deleted — its ID is cited
        from code and ADRs, and a missing ID reads as an editing slip."""
        text = REGISTRY.read_text()
        for qid, cells in _rows():
            joined = " ".join(cells).lower()
            if "retracted" in joined:
                assert "does not reproduce" in joined or "not reproducible" in joined, (
                    f"{qid} is marked retracted without saying what was re-measured"
                )
                assert re.search(r"20\d\d-\d\d-\d\d", joined), (
                    f"{qid} is retracted without a date — the next reader cannot tell "
                    f"whether the retest predates their own symptom"
                )


class TestContractProber:
    """The live prober's shape table (ADR-052).

    It runs against a real instance and so cannot run in CI, but its
    construction can: a prober that builds the wrong shapes, or that only
    ever asserts success, would report a healthy contract while measuring
    nothing.
    """

    def _probes(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "verify_contract", REPO / "scripts" / "verify_contract.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod, mod.build_probes("PROJ", "OPS")

    def test_probes_both_directions(self):
        """Positive-only probing cannot detect a retracted quirk."""
        mod, probes = self._probes()
        expectations = {p[3] for p in probes}
        assert expectations == {mod.OK, mod.REJECT}, (
            "the table must contain shapes expected to FAIL as well as succeed — "
            "a negative control that starts passing is how a stale quirk surfaces"
        )
        assert sum(1 for p in probes if p[3] == mod.REJECT) >= 3

    def test_known_bad_shapes_are_negative_controls(self):
        _, probes = self._probes()
        rejected = " ".join(q for _, _, q, e in probes if e == "reject")
        assert "resolved:" in rejected, "the broken alias must be probed (Q1)"
        assert " or project:" in rejected, "the OR-joined project form must be probed (Q17)"
        assert "due date:" in rejected, "the non-existent attribute must be probed"

    def test_relied_on_shapes_are_probed(self):
        _, probes = self._probes()
        accepted = " ".join(q for _, _, q, e in probes if e == "ok")
        assert "project: PROJ, OPS" in accepted, "the comma-list the code emits must be probed"
        assert "resolved date:" in accepted
        # Q2 is retracted — both orders must be asserted to work, so a
        # future parser change that reintroduces the constraint is caught.
        assert accepted.count("resolved date:") >= 2

    def test_project_keys_come_only_from_arguments(self):
        """No project key may be hard-coded — they are discovered at runtime.

        Checked behaviourally, with sentinels: an earlier version of this
        test listed the real keys in its own regex, which published the
        full set of internal project codes in a public repo. A test that
        has to name the secret to protect it is the wrong test.
        """
        import re

        mod, _ = self._probes()
        sentinels = {"AAA", "BBB"}
        for _, desc, query, _ in mod.build_probes(*sorted(sentinels)):
            for clause in re.findall(
                r"project:\s*([A-Za-z0-9_]+(?:\s*,\s*[A-Za-z0-9_]+)*)", query
            ):
                for key in filter(None, re.split(r"[,\s]+", clause.strip())):
                    assert key in sentinels, f"hard-coded project key {key!r} in {desc!r}"
            for ident in re.findall(r"#([A-Za-z0-9_]+)-\d+", query):
                assert ident in sentinels, f"hard-coded issue prefix {ident!r} in {desc!r}"
