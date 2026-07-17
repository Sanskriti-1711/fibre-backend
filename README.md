# FTTH Fiber Backend

Django backend for the FTTH (Fiber To The Home) HLD planning system.

## Project Setup

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

## Pipeline Samples

The `pipeline_samples/` directory contains large sample data files (Excel, GeoPackage, ZIP) used for testing the FTTH pipeline. These files are excluded from version control due to their size (>100 MB).

To run with sample data, place your pipeline input files in this directory.

## Database

- **Local dev**: Uses a Docker PostGIS instance with `FTTH_DB=local` env var
- **Production**: Remote PostgreSQL on Zeabur

## API

See `docs/apis.md` for API documentation.
