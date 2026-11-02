# microlms-core

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
uv add git+https://<git-host>/microlms-core --tag v0.1.0
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

## Scope and design limits

Decisions the core is built around. A project that needs something else needs a core change, not a client workaround - better to know before adopting:

- **Completion model.** A course is an ordered list of activities; each activity carries one content package per language, and is satisfied through any one of its language variants; the enrollment completes once every activity of the course is satisfied. A course may require other courses completed before enrollment (prerequisites), and a sequential course locks each activity until every earlier one is satisfied - gating is all-or-nothing per course: there are no optional activities, per-activity prerequisite graphs, course-level passing scores, or expiry/re-certification.
- **Language model.** Content exists per language as activity variants. Course and activity titles have one reference text (used where no language is known, e.g. LTI launches) plus optional per-language display texts, and the API returns every translation: the core never picks a language for the client, so what to show for a missing one is the client's choice.
- **Tenancy model.** Organizations are flat tenants: a user belongs to any number of orgs with its own org roles in each (`lms_org_membership`), and only platform roles live outside any org. Course visibility is by org assignment (a user sees what any of their orgs sees), and course authorship is by a single owning org (none = platform-owned). There is no org hierarchy. Single-tenant deployments create one org for their users and point `MICROLMS_REGISTRATION_ORG_SLUG` at it when users self-register.
- **Authorization model.** Modelled on Kubernetes RBAC. The core checks permissions, never role names: every core endpoint gates on one specific `microlms.Permission` (dot-separated strings like `lms.course.publish`), and a role is a set of concrete permissions (no wildcards, no deny rules). Scope belongs to the assignment, not the role: a role assigned inside an organization acts there only (courses scope by their owning org), so a user can be a manager in one org and a learner in another; the same role assigned at platform scope acts everywhere, and platform-global resources (orgs, audit, xAPI administration, LTI tool registration) need a platform assignment outright. Self-service is never permission-gated. No escalation: nobody assigns a role, or acts on an account, holding a permission they lack at that scope, so managers can hand out at most what they hold; `GET /api/v1/users/me/assignable-roles` lists what that leaves. Self-registration needs `MICROLMS_REGISTRATION_ORG_SLUG` (and guest xAPI ingestion `MICROLMS_XAPI_GUEST_ORG_SLUG`) to name an existing organization. Role vocabularies are fully client-defined via the `access_policy`, and client routers gate on their own permission strings, but which permission each *core* endpoint checks is fixed: core endpoints cannot be re-gated without replacing the router.
- **User model.** A user is an immutable UUID with a full name, an optional email (unique among users) and an optional preferred language, holding platform roles and org roles as described above. The email is a plain profile field: the email/password sign-in logs users in by it and keeps only the password and lockout state in `lms_password_credential`, the OIDC sign-in fills it from the provider's claim, and anonymous users or identities whose provider sends none have no email. Client-specific profile data goes in client-owned satellite tables keyed on the user id; the core user endpoints will not expose it.

Client migrations live in the client repo as a second Alembic chain. In the client `alembic.ini`:

```ini
script_location = migrations
version_locations = migrations/versions microlms:db/migrations/versions
path_separator = space
```

`alembic upgrade heads` then applies both chains. Client tables may reference core tables by foreign key but never alter them.

## License

Copyright (C) 2026 LMDDC. Licensed under the GNU Affero General Public License v3.0 or later (`AGPL-3.0-or-later`), see `LICENSE`. Bundled third-party components keep their own licenses, listed in `THIRD_PARTY_NOTICES.md`.
