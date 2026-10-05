# FTTH Fiber Backend

Django backend for the FTTH (Fiber To The Home) HLD planning system.

## Project Setup

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_dev_admin
python manage.py runserver
```

### Login account

`migrate` creates the `users_user` table but no rows, so until you seed a user
`POST /api/users/login/` answers
`400 {"non_field_errors": ["Invalid credentials"]}` for **every** credential —
including the `admin@admin.com` / `admin123$` pair shown as an example request
body in `docs/apis.md` and `docs/admin-user-flow.md`. Those docs illustrate the
request shape; they do not describe a seeded account.

`seed_dev_admin` creates that account so the documented example actually works:

```bash
python manage.py seed_dev_admin                          # admin@admin.com / admin123$
python manage.py seed_dev_admin --email lead@team.com --password 'other-pw'
python manage.py seed_dev_admin --reset                  # update an existing account
```

It is idempotent (an existing account is left alone unless `--reset`) and
refuses to run when `DEBUG` is False, since it writes a known password.

> **Windows / QGIS note:** if a global `PYTHONPATH` points at a QGIS Python 3.12
> `site-packages`, it shadows anaconda's 3.11 and `manage.py` fails with
> `fields.E210: Cannot use ImageField because Pillow is not installed` — Pillow
> *is* installed, the QGIS copy just cannot be loaded. Clear it for the command:
> `set PYTHONPATH=` (cmd) or `$env:PYTHONPATH=""` (PowerShell).

## Pipeline Samples

The `pipeline_samples/` directory contains large sample data files (Excel, GeoPackage, ZIP) used for testing the FTTH pipeline. These files are excluded from version control due to their size (>100 MB).

To run with sample data, place your pipeline input files in this directory.

## Database

- **Local dev**: Uses a Docker PostGIS instance with `FTTH_DB=local` env var
- **Production**: Remote PostgreSQL on Zeabur

## API

See `docs/apis.md` for API documentation.
