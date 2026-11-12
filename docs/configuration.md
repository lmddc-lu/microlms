# Configuration

Configuration holds the values that change from one deployment to another: where the database
and the Learning Record Store are, which organization new users join, the key the LTI platform
signs with, and so on. They are the fields of `microlms.Settings`, the object your project passes
to `create_app(settings=...)` (see [Get started](get-started.md)).

## Loading settings

`Settings` reads each field from an environment variable named after it, in upper case with the
`MICROLMS_` prefix, or from a `.env` file in the working directory. For example, the
[anonymous SCORM demo](https://github.com/lmddc-lu/microlms/tree/main/demo/anonymous-scorm),
running against a local PostgreSQL and LRS, uses:

```bash
MICROLMS_DATABASE_URL=postgresql+psycopg://lms:lms@localhost:5432/lms
MICROLMS_LRS_URL=http://localhost:8080
MICROLMS_LRS_API_KEY=demo_key
MICROLMS_LRS_API_SECRET=demo_secret
# Organization anonymous visitors register into, seeded by the demo migration
MICROLMS_REGISTRATION_ORG_SLUG=playground
```

## Adding your own settings

Subclass `Settings` to add the fields your project needs. They are read the same way, with the
same prefix:

```python
from microlms import Settings


class AppSettings(Settings):
    smtp_host: str  # read from MICROLMS_SMTP_HOST
```

## What each setting is for

Only `MICROLMS_DATABASE_URL` is required. The others are needed only by the features that use
them. Details and accepted formats are in the docstring of each field in
[`src/microlms/config.py`](https://github.com/lmddc-lu/microlms/blob/main/src/microlms/config.py).

| Variable | Used for |
| --- | --- |
| `MICROLMS_DATABASE_URL` | PostgreSQL database URL. Required. |
| `MICROLMS_APP_NAME` | Application title, shown in the OpenAPI docs and the `/health` response. |
| `MICROLMS_DEBUG` | Tracebacks in error responses. Never in production. |
| `MICROLMS_API_PREFIX` | URL prefix of the core routers and content modules (default `/api/v1`). The auth routes stay at `/auth`. |
| `MICROLMS_LRS_URL`, `MICROLMS_LRS_API_KEY`, `MICROLMS_LRS_API_SECRET` | The Learning Record Store where the content modules (`scorm`, `cmi5`, `h5p`, `lti`, `xapi`) record learner activity, and its credentials. |
| `MICROLMS_LRS_ACTOR_HOME_PAGE` | Stable address identifying your users in xAPI statements. Set it once per deployment. |
| `MICROLMS_LAUNCH_MAX_AGE` | How long a content launch stays valid at most (default one day). |
| `MICROLMS_REGISTRATION_ORG_SLUG` | Organization self-registered users join. Unset, self-registration is refused. |
| `MICROLMS_XAPI_INGEST_KEYS` | Keys trusted external sources present to push xAPI statements (`xapi` module). |
| `MICROLMS_XAPI_GUEST_ROLE`, `MICROLMS_XAPI_GUEST_ORG_SLUG` | Role and organization of guests pushing xAPI statements without an account (`xapi` module). Unset, guest pushes are refused. |
| `MICROLMS_LTI_ISSUER`, `MICROLMS_LTI_PRIVATE_KEY` | Identifier and signing key of the LTI 1.3 platform (`lti` module). |

## What is not configured here

The authentication backend, the storage backend, the event hooks and the access policy are not
settings: your project builds them and passes them to `create_app()`, so their options are
arguments of their constructors.
