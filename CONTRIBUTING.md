# Contributing to yt-mcp

Thanks for your interest in contributing!

## Quick start

```bash
git clone https://github.com/velesnitski/yt-mcp.git
cd yt-mcp
python -m venv .venv && source .venv/bin/activate
pip install -e ".[test]"
pytest
```

## Development workflow

1. Fork the repo and create a branch from `dev`
2. Make your changes
3. Add tests if adding new tools or scoring logic
4. Run `pytest` — all tests must pass
5. Open a PR against `dev`

## Code style

- Python 3.10+
- No external linter enforced — just be consistent with existing code
- Precompile regex at module level
- Use `asyncio.gather` for independent API calls
- Every tool gets an `instance: str = ""` parameter

## Adding a new tool

1. Add the function in the appropriate module under `src/yt_mcp/tools/`
2. The `@logged` decorator is applied automatically at registration
3. Update `WRITE_TOOLS` in `__init__.py` if the tool modifies data
4. Update `tests/test_registration.py` with the new tool count and name
5. Update `tests/test_server.py` tool count
6. Update `README.md` tool table and count

## Reporting bugs

Open an issue with:
- What you did
- What you expected
- What happened (include error message if available)
- Your YouTrack version (Cloud or self-hosted + version)

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
