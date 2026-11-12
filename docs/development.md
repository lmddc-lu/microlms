# Development

Working on the core itself, from a clone of the repository:

```bash
uv sync --all-extras               # the tests cover every optional module
cp .env.example .env
uv run uvicorn main:app --reload   # dev server
uv run pytest                      # tests (needs Docker)
uv run ruff check                  # lint
```

`main.py` is a dev-only entrypoint; projects using microlms never use it.

New core migration:

```bash
uv run alembic upgrade heads
uv run alembic revision --autogenerate -m "message" --head core@head
```
