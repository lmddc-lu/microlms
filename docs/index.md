# microLMS core

The base for an LMS platform: users, organizations, courses, enrollments and content players,
served over an HTTP API. Provide a small wrapper around it and a frontend, and you are up and
running with an LMS hosting courses for learners.

The wrapper is your own Python project: it installs this package, calls `create_app()` with its
settings and backends (authentication, file storage, event hooks), and can mount its own
endpoints on top.

Distributed as `microlms-core`, imported as `microlms`.

Source code: [github.com/lmddc-lu/microlms](https://github.com/lmddc-lu/microlms)

## Features

- **Users, organizations, courses and enrollments** over a FastAPI HTTP API. A course is an
  ordered list of activities (optionally sequential), each available in several languages.
- **Permission-based authorization** modelled on Kubernetes RBAC: roles are project-defined sets
  of permissions, assigned either platform-wide or inside one organization, with no privilege
  escalation.
- **Content modules**, each behind its own extra:
    - `scorm` - SCORM 1.2 and 2004 packages, played by the bundled
      [scorm-again](https://github.com/jcputney/scorm-again) runtime
    - `cmi5` - cmi5 packages
    - `h5p` - `.h5p` packages exported with their libraries, played by the bundled
      [h5p-standalone](https://github.com/tunapanda/h5p-standalone) player
    - `lti` - the platform side of LTI 1.3, launching external tools and receiving their scores
    - `xapi` - ingestion of xAPI statements pushed by trusted external sources
- **xAPI everywhere**: every module records learner activity in an external Learning Record
  Store, server-side (LRS credentials never reach the browser).
- **Pluggable authentication**: bundled JWT (password and refresh token grants, browser session
  cookie) and OpenID Connect backends, or your own `AuthBackend`.
- **Pluggable storage**: `FilesystemStorage` by default, or your own `StorageBackend` (e.g. an
  object store).
- **Typed event hooks** fired after commit (user created, enrolled, course completed, ...),
  periodic background tasks, and an audit log.

## Requirements

- Python 3.13+
- PostgreSQL (the only supported database)
- An xAPI Learning Record Store such as [SQL LRS](https://github.com/yetanalytics/lrsql), for the
  `scorm`, `cmi5`, `h5p`, `lti` and `xapi` modules

## Installation

Depend on a tagged release, with the extras you need:

```bash
uv add git+https://github.com/lmddc-lu/microlms --tag v0.1.0 --extra jwt --extra scorm
```

| Extra | Enables |
| --- | --- |
| `jwt` | `microlms.auth.jwt.JwtAuthBackend` (password sign-in) |
| `oidc` | `microlms.auth.oidc.OidcAuthBackend` (OpenID Connect) |
| `scorm` | the `scorm` content module |
| `cmi5` | the `cmi5` content module |
| `h5p` | the `h5p` content module |
| `lti` | the `lti` module (LTI 1.3 platform) |
| `xapi` | the `xapi` ingestion module |

Then build the app as described in [Get started](get-started.md).

## Demos

Runnable projects built on the public core surface, under `demo/` in the repository:

- [`demo/anonymous-scorm`](https://github.com/lmddc-lu/microlms/tree/main/demo/anonymous-scorm) -
  anonymous learners upload and play SCORM packages.

## License

Copyright (C) 2026 LMDDC. Licensed under the GNU Affero General Public License v3.0 or later
(`AGPL-3.0-or-later`). Bundled third-party components keep their own licenses, listed in
[`THIRD_PARTY_NOTICES.md`](https://github.com/lmddc-lu/microlms/blob/main/THIRD_PARTY_NOTICES.md).
