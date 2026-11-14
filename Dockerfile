FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/qwqbzmz/term-rewind-" \
      org.opencontainers.image.description="Command-scoped filesystem snapshots and selective undo" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1
WORKDIR /opt/term-rewind
COPY pyproject.toml README.md LICENSE rewind.py timeline.py mcp_server.py ./
RUN python -m pip install --no-cache-dir .

WORKDIR /workspace
ENTRYPOINT ["rewind", "--store", "/snapshots"]
