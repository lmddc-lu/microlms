# Migrations

Core migrations ship inside the package as an Alembic chain with branch label `core`. Client
migrations live in the client repo as a second chain. In the client `alembic.ini`:

```ini
script_location = migrations
version_locations = migrations/versions microlms:db/migrations/versions
path_separator = space
```

`alembic upgrade heads` then applies both chains. Client tables may reference core tables by
foreign key but never alter them.
