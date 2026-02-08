from django.db import connections


def execute_gis_sql(sql, params=None):

    with connections["gis"].cursor() as cursor:
        cursor.execute(sql, params or [])


def create_layer_table(project_id, layer_name, geometry_type, srid):

    table_name = f"project_{project_id}_{layer_name}"

    geom_map = {
        "POINT": "Point",
        "LINESTRING": "LineString",
        "POLYGON": "Polygon",
    }

    if geometry_type not in geom_map:
        raise ValueError(f"Unsupported geometry type: {geometry_type}")

    sql = f"""
    CREATE TABLE IF NOT EXISTS {table_name} (
        id SERIAL PRIMARY KEY,
        geom geometry({geom_map[geometry_type]}, {srid})
    );

    CREATE INDEX IF NOT EXISTS {table_name}_geom_idx
    ON {table_name} USING GIST (geom);
    """

    execute_gis_sql(sql)
    return table_name
