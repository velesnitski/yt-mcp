# 063 — One version everywhere it is written

## Context

The server announces `__version__` — in `--version` and in the
instructions header every client receives. Releases 1.34.2 and 1.34.3
bumped `pyproject.toml` and `uv.lock` but left `src/yt_mcp/__init__.py` at
1.34.1, so both builds identified themselves as a release they were not.
Neither got a GitHub Release either, and the local client stayed pinned to
1.34.1 — so the certifi CA-bundle fix shipped in 1.34.2 was never actually
running here.

All three follow from one cause: those releases were cut by hand rather
than with `scripts/release.sh`, whose `prepare` step bumps every version
location and whose `ship` step creates the release and re-pins the client.
Nothing else noticed — CI was green, because no test compared the version
locations.

## Decision

- A test asserts that `pyproject.toml`, `uv.lock` and `__version__` agree,
  so a skew fails CI regardless of how a release is produced. It parses
  with regexes, since `tomllib` is unavailable on Python 3.10, the floor
  of the test matrix.
- `release.sh prepare` now checks that `__init__.py` and `pyproject.toml`
  agree **before** editing either. It read the current version from
  `__init__.py` and substituted that string in `pyproject.toml`; after the
  skew that substitution silently matched nothing, the next line still
  rewrote `__init__.py`, and the tree was left half-bumped. A skewed release
  therefore broke the next proper one.
- Release 1.34.4 through the script: all locations aligned, a GitHub
  Release published, and the client re-pinned so the CA fix is live.

## Consequences

- The test failed on `main` before this change and passes after it; it is
  verified against the defect it exists to catch.
- A manual release can still skip creating a GitHub Release or re-pinning
  the client. The test covers the part that misinforms clients; the rest
  remains the script's job.
