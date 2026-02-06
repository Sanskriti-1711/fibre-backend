from .postgis_service import execute_gis_sql, create_layer_table
from projects.models import ProjectLayer, FeatureStatus

def register_layer(
    project,
    import_session,
    layer_name,
    geometry_type,
    srid,
):
    table_name = create_layer_table(
        project.id,
        layer_name,
        geometry_type,
        srid
    )

    return ProjectLayer.objects.create(
        project=project,
        import_session=import_session,
        layer_name=layer_name,
        table_name=table_name,
        geometry_type=geometry_type,
        srid=srid,
    )
