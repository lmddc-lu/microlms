# Get started

A client project depends on `microlms-core` (see [Installation](index.md#installation)), builds
the app with `create_app()`, and mounts its own routers on top.

## Building the app

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

The running app serves its OpenAPI reference at `/docs`.

## Public surface

The nine `create_app` parameters are the whole supported customization surface. Client code may
import only the names each core module lists in its `__all__` (`microlms.api.deps`,
`microlms.events` and the `microlms.domain.*` services among them) and must not import or patch
core internals; anything that does not fit an extension point is a feature request on the core.

## Composing routers

Any subset of core routers composes: routers depend only on `microlms.domain` services and
`microlms.api.deps`, never on each other, so deselecting one only removes its endpoints (its
tables and domain services stay available). A client that needs a different API shape for a
feature deselects the core router and mounts its own built on the same domain services. Core
routers mount before client routers, so a client route on an already-taken path never wins: to
change one core endpoint, deselect that core router and remount its replacement.
