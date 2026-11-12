# Get started

Your project depends on `microlms-core` (see [Installation](index.md#installation)), builds the
app with `create_app()`, and mounts its own routers on top.

The [anonymous SCORM demo](https://github.com/lmddc-lu/microlms/tree/main/demo/anonymous-scorm)
is a good start to see how a project uses microlms: its `main.py` builds the app, `migrations/`
holds its own Alembic chain next to the core one, and `index.html` is a one-page frontend.

## Building the app

```python
from microlms import DEFAULT_ACCESS_POLICY, create_app, create_db_engine

settings = AppSettings()  # subclass of microlms.Settings
# One engine per process: hooks and periodic tasks open their sessions on it
# with microlms.create_session_factory(engine)
engine = create_db_engine(settings.database_url, echo=settings.debug)
app = create_app(
    settings=settings,
    engine=engine,  # sqlalchemy Engine the app runs on
    auth=SsoAuthBackend(),  # implements microlms.auth.AuthBackend
    storage=S3Storage(...),  # implements microlms.storage.StorageBackend
    hooks=AppHooks(),  # implements microlms.events.EventHooks
    routers=["users", "orgs", "courses", "enrollments", "audit"],  # core routers to mount
    modules=["scorm", "h5p"],  # optional content modules to mount
    schedules=[],  # periodic background tasks (microlms.PeriodicTask)
    access_policy=DEFAULT_ACCESS_POLICY,  # role -> permission set
)
app.include_router(app_router)  # your own endpoints
```

`AppSettings()` reads its values from `MICROLMS_`-prefixed environment variables or a `.env` file.
Only `MICROLMS_DATABASE_URL` is required; see [Configuration](configuration.md) for the rest.

The running app serves its OpenAPI reference at `/docs`.

## Database migrations

Core migrations ship inside the package as an Alembic chain with branch label `core`. Your
project's migrations live in your repo as a second chain. In your `alembic.ini`:

```ini
script_location = migrations
version_locations = migrations/versions microlms:db/migrations/versions
path_separator = space
```

`alembic upgrade heads` then applies both chains. Your tables may reference core tables by
foreign key but never alter them.

## Public surface

The nine `create_app` parameters are the whole supported customization surface. Your code may
import only the names each core module lists in its `__all__` (`microlms.api.deps`,
`microlms.events` and the `microlms.domain.*` services among them) and must not import or patch
core internals; anything that does not fit an extension point is a feature request on the core.

## Composing routers

Any subset of core routers composes: routers depend only on `microlms.domain` services and
`microlms.api.deps`, never on each other, so deselecting one only removes its endpoints (its
tables and domain services stay available). If you need a different API shape for a feature,
deselect the core router and mount your own built on the same domain services. Core routers
mount before yours, so a route of yours on an already-taken path never wins: to change one core
endpoint, deselect that core router and mount its replacement.
