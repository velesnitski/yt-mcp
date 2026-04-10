FROM python:3.12-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md uv.lock* ./
COPY src/ src/
RUN pip install --no-cache-dir .

FROM python:3.12-slim

RUN groupadd --gid 1000 mcp \
    && useradd --uid 1000 --gid mcp --shell /bin/sh --create-home mcp

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin/yt-mcp /usr/local/bin/yt-mcp

USER mcp
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/sse')" || exit 1

ENTRYPOINT ["yt-mcp"]
CMD ["--transport", "sse", "--port", "8000"]
