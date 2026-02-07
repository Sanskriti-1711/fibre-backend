import uuid
from django.utils.timezone import now
from projects.models import (
    ImportSession,
    ProjectLayer,
    FeatureStatus,
)
from .gpkg_reader import list_layers, inspect_layer
from .postgis_service import execute_gis_sql, create_layer_table
import fiona

def import_geopackage(project, uploaded_file):
    # 1. Save file
    filename = f"{uuid.uuid4()}.gpkg"
    file_path = f"media/gpkg_uploads/{filename}"

    with open(file_path, "wb+") as dest:
        for chunk in uploaded_file.chunks():
            dest.write(chunk)

    # 2. Create import session
    session = ImportSession.objects.create(
        project=project,
        original_filename=uploaded_file.name,
        stored_file_path=file_path,
        status="uploaded",
    )

    # 3. Read layers
    layers = list_layers(file_path)

    created_layers = []

    for layer_name in layers:
        info = inspect_layer(file_path, layer_name)

        geometry_type = info["geometry_type"]
        srid = int(info["crs_epsg"] or 27700)

        # 4. Create PostGIS table
        table_name = create_layer_table(
            project.id,
            layer_name,
            geometry_type,
            srid,
        )

        # 5. Insert geometries
        feature_pks = []

        with fiona.open(file_path, layer=layer_name) as src:
            for feature in src:
                geom_wkt = feature["geometry"]
                sql = f"""
                    INSERT INTO {table_name} (geom)
                    VALUES (ST_SetSRID(ST_GeomFromGeoJSON(%s), %s))
                    RETURNING id;
                """
                execute_gis_sql(sql, [str(geom_wkt), srid])

        feature_count = info["feature_count"]

        # 6. Register layer
        layer = ProjectLayer.objects.create(
            project=project,
            import_session=session,
            layer_name=layer_name,
            table_name=table_name,
            geometry_type=geometry_type,
            srid=srid,
            feature_count=feature_count,
        )

        # 7. Create feature_status rows
        FeatureStatus.objects.bulk_create([
            FeatureStatus(
                project=project,
                project_layer=layer,
                feature_pk=str(i + 1),
                status="pending",
            )
            for i in range(feature_count)
        ])

        created_layers.append(layer_name)

    # 8. Update session + project
    session.status = "imported"
    session.validation_summary = {
        "layers": created_layers,
        "layer_count": len(created_layers),
    }
    session.save()

    project.last_activity_at = now()
    project.status = "in_progress"
    project.save(update_fields=["last_activity_at", "status"])

    return session
