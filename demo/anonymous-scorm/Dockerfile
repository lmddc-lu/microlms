# Built from the repository root (see docker-compose.yml), so the demo
# installs the core from this checkout
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

ENV UV_LINK_MODE=copy

COPY . /app
WORKDIR /app/demo/anonymous-scorm
RUN uv sync --frozen

CMD ["sh", "-c", "uv run --no-sync alembic upgrade heads && uv run --no-sync uvicorn main:app --host 0.0.0.0 --port 8000"]
