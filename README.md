# microlms

Reusable micro-LMS engine. Client projects install this package, call `create_app()` with their own backends, and mount their own routers on top. The core is the only shared code; client repos stay thin shells.

Distributed as `microlms-core`, imported as `microlms`. Runs on PostgreSQL, records learner activity in an xAPI Learning Record Store, and plays SCORM, cmi5 and H5P content and LTI 1.3 tools through optional modules.

## Documentation

- [Features, requirements and installation](docs/index.md)
- [Get started](docs/get-started.md) 
- [Configuration](docs/configuration.md)
- [Migrations](docs/migrations.md)
- [Development](docs/development.md)

## License

Copyright (C) 2026 LMDDC. Licensed under the GNU Affero General Public License v3.0 or later (`AGPL-3.0-or-later`), see `LICENSE`. Bundled third-party components keep their own licenses, listed in `THIRD_PARTY_NOTICES.md`.
