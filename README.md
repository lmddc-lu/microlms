# microlms

The base for an LMS platform: users, organizations, courses, enrollments and content players, served over an HTTP API. Provide a small wrapper around it and a frontend, and you are up and running with an LMS hosting courses for learners.

The wrapper is your own Python project: it installs this package, calls `create_app()` with its settings and backends (authentication, file storage, event hooks), and can mount its own endpoints on top.

Distributed as `microlms-core`, imported as `microlms`. Runs on PostgreSQL, records learner activity in an xAPI Learning Record Store, and plays SCORM, cmi5 and H5P content and LTI 1.3 tools through optional modules.

## Documentation

The documentation is hosted at [documentation.skilltech.tools/microlms-core-documentation](https://documentation.skilltech.tools/microlms-core-documentation/). Its sources are in [`docs/`](docs):

- [Features, requirements and installation](docs/index.md)
- [Get started](docs/get-started.md) 
- [Configuration](docs/configuration.md)
- [Development](docs/development.md)

## License

Copyright (C) 2026 LMDDC. Licensed under the GNU Affero General Public License v3.0 or later (`AGPL-3.0-or-later`), see `LICENSE`. Bundled third-party components keep their own licenses, listed in `THIRD_PARTY_NOTICES.md`.
