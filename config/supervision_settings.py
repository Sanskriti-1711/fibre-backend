"""Development against the shared PostgreSQL, isolated in a `supervision` schema.

The supervision module runs on the same server and credentials the deployment
uses (PG* from `.env`), but every table it creates and reads lives in its own
`supervision` schema. The platform's `business` and `gis` schemas are never
written to, so a local experiment cannot change a production row.

    python manage.py migrate users projects assignments survey notifications \\
        --settings=config.supervision_settings

`dev/supervision-db.sh` creates the schema and runs exactly that command.
`config.settings` remains the deployment configuration; this module only
overrides the database and turns on the local conveniences `dev_settings`
already defines (DEBUG, permissive CORS, console email, frontend hosting).
"""

from django.core.exceptions import ImproperlyConfigured

from .dev_settings import *  # noqa: F401,F403

# Which schema this module owns. Overridable (SUPERVISION_DB_SCHEMA) so a second
# checkout can work in its own schema without editing code — validated, because
# the value is pasted into the connection options string.
_SCHEMA = os.getenv('SUPERVISION_DB_SCHEMA', 'supervision')  # noqa: F405
if not _SCHEMA.replace('_', '').isalnum() or _SCHEMA[:1].isdigit():
    raise ImproperlyConfigured(
        'SUPERVISION_DB_SCHEMA must be a plain identifier (letters, digits, underscore).'
    )

# Same server, same credentials, different search_path. `public` stays on the
# path only for PostGIS's own catalog tables (geometry_columns,
# spatial_ref_sys): it holds no Django table, so nothing here can read or write
# a platform row by accident.
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.getenv('PGDATABASE', 'zeabur'),  # noqa: F405
        'USER': os.getenv('PGUSER', 'root'),  # noqa: F405
        'PASSWORD': os.getenv('PGPASSWORD'),  # noqa: F405
        'HOST': os.getenv('PGHOST', '91.98.18.217'),  # noqa: F405
        'PORT': os.getenv('PGPORT', '32467'),  # noqa: F405
        'OPTIONS': {
            'options': f'-c search_path={_SCHEMA},public',
            # Remote database: bound the connect so a stall cannot pin the
            # dev server's only worker.
            'connect_timeout': 10,
        },
    }
}
