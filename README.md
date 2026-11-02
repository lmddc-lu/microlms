# microlms

Reusable micro-LMS engine. Client projects install this package, call `create_app()` with their own backends, and mount their own routers on top. The core is the only shared code; client repos stay thin shells.

## Development

```sh
uv sync
cp .env.example .env
uv run uvicorn main:app --reload   # dev server
uv run pytest                      # tests
```

Core migrations (branch label `core`, shipped inside the package):

```sh
uv run alembic upgrade heads
uv run alembic revision --autogenerate -m "message" --head core@head
```

## Demos

Runnable client projects built on the public core surface, under `demo/`:

- [`demo/anonymous-scorm`](demo/anonymous-scorm) - anonymous learners upload and play SCORM packages.

## Usage in a client project

Depend on a tagged release:

```sh
uv add git+https://github.com/lmddc-lu/microlms --tag v0.1.0
```

Build the app (see `main.py` for the dev equivalent):

```python
from microlms import DEFAULT_ACCESS_POLICY, create_app, create_db_engine

settings = ClientSettings()           # subclass of microlms.Settings
# One engine per process: hooks and periodic tasks open their sessions on it
# with microlms.create_session_factory(engine)
engine = create_db_engine(settings.database_url, echo=settings.debug)
app = create_app(
    settings=settings,
    engine=engine,                    # sqlalchemy Engine the app runs on
    auth=ClientSSOAuth(),             # implements microlms.auth.AuthBackend
    storage=S3Storage(...),           # implements microlms.storage.StorageBackend
    hooks=ClientHooks(),              # implements microlms.events.EventHooks
    routers=["users", "orgs", "courses", "enrollments", "audit"],  # core routers to mount
    modules=["scorm", "h5p"],         # optional content modules to mount
    schedules=[],                     # periodic background tasks (microlms.PeriodicTask)
    access_policy=DEFAULT_ACCESS_POLICY,  # role -> permission set
)
app.include_router(client_router)     # client-specific endpoints
```

The nine `create_app` parameters are the whole supported customization surface. Client code may import only the names each core module lists in its `__all__` (`microlms.api.deps`, `microlms.events` and the `microlms.domain.*` services among them) and must not import or patch core internals; anything that does not fit an extension point is a feature request on the core.

Any subset of core routers composes: routers depend only on `microlms.domain` services and `microlms.api.deps`, never on each other, so deselecting one only removes its endpoints (its tables and domain services stay available). A client that needs a different API shape for a feature deselects the core router and mounts its own built on the same domain services. Core routers mount before client routers, so a client route on an already-taken path never wins: to change one core endpoint, deselect that core router and remount its replacement.

Client migrations live in the client repo as a second Alembic chain. In the client `alembic.ini`:

```ini
script_location = migrations
version_locations = migrations/versions microlms:db/migrations/versions
path_separator = space
```

`alembic upgrade heads` then applies both chains. Client tables may reference core tables by foreign key but never alter them.

## License

Copyright (C) 2026 LMDDC. Licensed under the GNU Affero General Public License v3.0 or later (`AGPL-3.0-or-later`), see `LICENSE`. Bundled third-party components keep their own licenses, listed in `THIRD_PARTY_NOTICES.md`.
