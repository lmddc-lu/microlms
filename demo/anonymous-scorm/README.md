# Anonymous SCORM demo

A minimal client of microlms-core: visitors get an anonymous account on their first visit, upload SCORM packages (1.2 or 2004) and play them in the core's SCORM player. One page, no build step.

It shows a client wired through the public core surface only:

- `main.py` - `create_app` with `AnonymousAuthBackend`, the `scorm` module, and an access policy that lets `learner` create and publish courses, import packages and enroll.
- `migrations/` - the demo's own Alembic chain, next to the core chain, seeding the `playground` organization visitors register into (`MICROLMS_REGISTRATION_ORG_SLUG`).
- `index.html` - the page: registers anonymously (the token is kept in `localStorage`), uploads a package as a new published course, enrolls on first play and embeds the player, ending its session through the player's `microlms:scorm:end` message before returning to the list.

**Local use only.** Any visitor can upload a package, and package scripts run on the app origin with the visitor's credentials.

## Run with docker compose

From this directory:

```sh
docker compose up -d --build
```

Open http://localhost:8000 and upload a SCORM zip (with `imsmanifest.xml` at its root). Services: `app` (port 8000, builds the core from this checkout and applies both migration chains on start), `db` (postgres 17, with the `lms` and `lrsql_db` databases) and `lrs` (lrsql, port 8080). The statements the player produces land in lrsql:

```sh
curl -u demo_key:demo_secret -H 'X-Experience-API-Version: 1.0.3' \
  'http://localhost:8080/xapi/statements?limit=5'
```

## Run the app on the host

With `db` and `lrs` from docker compose:

```sh
docker compose up -d db lrs
cp .env.example .env
uv run alembic upgrade heads
uv run uvicorn main:app --reload
```

The demo depends on the core of this checkout as an editable path dependency, so core edits apply on reload.
