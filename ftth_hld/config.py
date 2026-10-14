"""
Configuration for the FTTH HLD Django module.

All settings can be overridden via environment variables prefixed with
``FTTH_``.
"""

import os


# ---------------------------------------------------------------------------
# FastAPI Engine — the single pipeline orchestrator
# ---------------------------------------------------------------------------
# The Django ftth_hld app proxies all pipeline operations to this service.
# The engine (HLD_Planning_01/web/backend) handles Docker exec/cp for
# qgis_process.
#
# Resolution order:
#   1. FTTH_ENGINE_URL env var (always wins — set this in the deployment
#      environment if the production engine moves).
#   2. Local development (FTTH_DB=local|dev|docker — same flag settings.py
#      uses) → http://127.0.0.1:8080 (the FastAPI engine started from
#      HLD_Planning_01/web/backend).
#      NOTE: 127.0.0.1 is used instead of localhost on purpose — on Windows
#      ``localhost`` resolves to IPv6 ::1 first, and every HTTP request burns
#      ~2s waiting for the IPv6 connect to fail before falling back to IPv4.
#      With ~40 file fetches per design-package download that added ~80s.
#   3. Production default → https://ftth-planning.onrender.com (the live
#      engine built from the sanskriti17/ftth_planning Docker image).
def _default_engine_url() -> str:
    if os.getenv('FTTH_DB', '').lower() in ('local', 'dev', 'docker'):
        return 'http://127.0.0.1:8080'
    # Local development (DEBUG on) defaults to the local FastAPI engine so the
    # results map works even when the server is started without FTTH_ENGINE_URL.
    try:
        from django.conf import settings

        if getattr(settings, 'DEBUG', False):
            return 'http://127.0.0.1:8080'
    except Exception:
        pass
    return 'https://ftth-planning.onrender.com'


FTTH_ENGINE_URL = os.getenv('FTTH_ENGINE_URL', _default_engine_url()).rstrip('/')

# ---------------------------------------------------------------------------
# Pipeline stages
# Each stage has:
#   index      – 0‑based order
#   name       – human‑readable label
#   algorithm  – substring matched in qgis_process log output
#   outputs    – GPKG filename stems (all must exist for the stage to count
#                as complete)
# ---------------------------------------------------------------------------
STAGES = [
    {'index': 0, 'name': 'Object Layer', 'algorithm': '01_object_layer', 'outputs': ['Objects']},
    {'index': 1, 'name': 'Polygon Layer', 'algorithm': '02_polygon_layer', 'outputs': ['Polygons']},
    {
        'index': 2,
        'name': 'Network Layer',
        'algorithm': '03_network_layer',
        'outputs': ['PDPs', 'MFG'],
    },
    {
        'index': 3,
        'name': 'Trench Layer',
        'algorithm': '04_trench_layer',
        'outputs': ['Final_Trenches'],
    },
    # Phase C cascade (TRENCH_DESIGN.md §6.1): ducts are laid before the
    # cables are pulled, so the duct stage carries the lower index.
    {
        'index': 4,
        'name': 'Duct Layer',
        'algorithm': '05_duct_layer',
        'outputs': ['Feeder_Ducts', 'Distribution_Ducts'],
    },
    {
        'index': 5,
        'name': 'Cable Layer',
        'algorithm': '06_cable_layer',
        'outputs': ['Feeder_Cable', 'Distribution_Cable'],
    },
]

# Maps API-friendly layer name → (GPKG stem, internal layer name).
# Individual sub-layers (feeder_cable, distribution_ducts, etc.) each get
# their own entry so the frontend can fetch them independently by name.
LAYER_NAME_MAP = {
    # Canonical names matching the backend ONECLICK_OUTPUTS
    'objects': ('Objects', 'object_layer'),
    'polygons': ('Polygons', 'polygon_layer'),
    'pdps': ('PDPs', 'PDPs'),
    'mfg': ('MFG', 'MFG'),
    'mfg_service_areas': ('MFG_Service_Areas', 'MFG_Service_Areas'),
    'feeder_cable': ('Feeder_Cable', 'Feeder_Cable'),
    'distribution_cable': ('Distribution_Cable', 'Distribution_Cable'),
    'feeder_ducts': ('Feeder_Ducts', 'Feeder_Ducts'),
    'distribution_ducts': ('Distribution_Ducts', 'Distribution_Ducts'),
    'drop_ducts': ('Drop_Ducts', 'Drop_Ducts'),
    'coupleurs': ('Coupleurs', 'Coupleurs'),
    'chambers': ('Chambers', 'Chambers'),
    'poles': ('Poles', 'Poles'),
    # Aerial legs the trench stage classified (never excavated): a design
    # decision, separate from the aerial drop the pole/aerial stage builds.
    'aerial_drops': ('Aerial_Drops', 'aerial_drops'),
    # What the pole / aerial-drop stage BUILDS for those classified legs: the
    # overhead span (never dug) and the cable it carries. Both are published by
    # the engine and ingested into `gis.aerial_span_layer` / `gis.aerial_cable_layer`,
    # but they were missing from this map — and `LayerGeoJSONView` rejects any
    # name that is not in it, so the two layers 404'd on the platform and could
    # never be drawn, in any project. That is why the aerial routes were
    # invisible on the map even on a run with a real aerial classification.
    #
    # Renamed from `aerial_drop_trenches` / `Aerial_Drop_Trenches`: a trench is
    # an excavation and this is not (EXCAVATION=0, Overhead). The old public
    # name is kept below as an alias so a stored FtthLayer row still resolves.
    'aerial_spans': ('Aerial_Spans', 'aerial_spans'),
    'aerial_drop_trenches': ('Aerial_Spans', 'aerial_drop_trenches'),
    'aerial_cable': ('Aerial_Cable', 'aerial_cable'),
    # The trench designer's structural nodes (HDD pits / junctions / PDPs /
    # bends / pulls): the evidence behind every planned chamber, and the layer
    # the map draws the structures' real positions from.
    'trench_nodes': ('Trench_Nodes', 'trench_nodes'),
    'brownfield': ('Existing_Infrastructure', 'brownfield'),
    'trenches': ('Final_Trenches', 'trench_layer'),
    # Built ON the platform, not by the engine: `permits.analysis.trench_sections`
    # expands `trenches` into one row per street-attributed section and persists
    # it into the HLD layer store as `trench_sections` (`SECTION_LAYER`). It was
    # absent from this map, and `LayerGeoJSONView` rejects any name that is not
    # in it — so a layer the platform derives, stores and needs (the permits
    # matrix keys its rows on the section) could never be fetched back out. The
    # engine has no file for it, so `get_layer_geojson`'s disk fallback returns
    # None and the stored row is what answers.
    'trench_sections': ('Trench_Sections', 'trench_sections'),
    # Backward-compatible aliases
    'network': ('Network', 'network_layer'),
    'cables': ('Feeder_Cable', 'cable_layer'),
    'ducts': ('Feeder_Ducts', 'duct_layer'),
}

# Maps an engine output **file stem** → the `gis.*` table that backs it.
#
# The engine's own output files live on the engine's disk, which is NOT
# duplicated on the host that serves this API. Every run also publishes its
# layers into the shared PostGIS database, and that is the store the results
# map already reads (`FtthLayer`, and `gis.*` straight from SQL). So when the
# engine cannot serve a file - a restarted container, a run executed on another
# machine, or a project whose engine-side exports predate a change - the
# download path falls back to this table and the layer is still delivered
# instead of silently vaporising from both packages.
#
# Stems are exactly as the engine names them in its ``downloads`` list, and
# every table here is one the engine creates (see `postgis.LAYER_TABLES`).
LAYER_STEM_GIS_TABLES = {
    'Objects': 'object_layer',
    'Polygons': 'polygon_layer',
    'PDPs': 'pdps',
    'MFG': 'mfg',
    'MFG_Service_Areas': 'mfg_service_areas',
    # Feeder/Distribution/Drop share one merged table, distinguishable by STAGE.
    'Feeder_Cable': 'cable_layer',
    'Distribution_Cable': 'cable_layer',
    'Aerial_Cable': 'aerial_cable_layer',
    'Feeder_Ducts': 'duct_layer',
    'Distribution_Ducts': 'duct_layer',
    'Drop_Ducts': 'duct_layer',
    'Final_Trenches': 'trench_layer',
    'Trench_Nodes': 'trench_nodes',
    'Chambers': 'chambers',
    'Coupleurs': 'coupleur_layer',
    'Poles': 'poles',
    'Existing_Infrastructure': 'brownfield',
    'Existing_Infrastructure_Points': 'brownfield',
    'Aerial_Spans': 'aerial_span_layer',
    'Aerial_Cable': 'aerial_cable_layer',
    'Aerial_Drops': 'aerial_drops',
}

# Stems that are SUB-LAYERS of one merged table: the feeder / distribution /
# drop ducts all live in `gis.duct_layer`, and the feeder / distribution cables
# in `gis.cable_layer`. They are told apart by the ``sublayer`` property the
# engine stamps on every row, so a fallback serving the whole table under
# `Feeder_Ducts.geojson` would hand back every duct in the design under a
# feeder-only name (810 rows where the engine's own file holds 522 + 288, or 6
# feeder cables against 359 cables). Filter on the property instead.
LAYER_STEM_SUBLAYER = {
    'Feeder_Cable': 'Feeder_Cable',
    'Distribution_Cable': 'Distribution_Cable',
    'Feeder_Ducts': 'Feeder_Ducts',
    'Distribution_Ducts': 'Distribution_Ducts',
    'Drop_Ducts': 'Drop_Ducts',
}

# Stems that share a table and are told apart by GEOMETRY rather than by a
# property. The brownfield layer publishes its lines (existing ducts, trenches)
# and its points (existing chambers, poles, cabinets) as two files, and the
# engine ingests both into the one `gis.brownfield` table — so serving the whole
# table under `Existing_Infrastructure_Points.geojson` would hand back the
# polygons AND the points (108 rows where the engine's own file holds 11), and a
# points-only layer would arrive carrying linework.
LAYER_STEM_GEOMETRY = {
    'Existing_Infrastructure': ('MULTILINESTRING', 'LINESTRING'),
    'Existing_Infrastructure_Points': ('MULTIPOINT', 'POINT'),
}

# Pipeline steps for step-by-step execution (matches HLD_Planning_01 ONECLICK_OUTPUTS)
PIPELINE_STEPS = [
    {
        'name': 'object',
        'alg_id': 'hldplanning:01_object_layer',
        'label': 'Object Layer',
        'outputs': ['Objects.gpkg'],
    },
    {
        'name': 'polygon',
        'alg_id': 'hldplanning:02_polygon_layer',
        'label': 'Polygon Layer',
        'outputs': ['Polygons.gpkg'],
    },
    {
        'name': 'network',
        'alg_id': 'hldplanning:03_network_layer',
        'label': 'Network Layer',
        'outputs': ['PDPs.gpkg', 'MFG.gpkg'],
    },
    {
        'name': 'trench',
        'alg_id': 'hldplanning:04_trench_layer',
        'label': 'Trench Layer',
        'outputs': ['Final_Trenches.gpkg'],
    },
    {
        'name': 'cable',
        'alg_id': 'hldplanning:06_cable_layer',
        'label': 'Cable Layer',
        'outputs': ['Feeder_Cable.gpkg', 'Distribution_Cable.gpkg'],
    },
    {
        'name': 'duct',
        'alg_id': 'hldplanning:05_duct_layer',
        'label': 'Duct Layer',
        'outputs': [
            'Feeder_Ducts.gpkg',
            'Distribution_Ducts.gpkg',
            'Drop_Ducts.gpkg',
            'Coupleurs.gpkg',
        ],
    },
]

# Step dependency chain: which step must be completed before this one
STEP_DEPENDENCIES = {
    'object': None,
    'polygon': 'object',
    'network': 'polygon',
    'trench': 'network',
    'cable': 'trench',
    'duct': 'trench',
}

# ======================================================================
# FIELD SURVEY PACKAGE — compact subset a surveyor needs in the field.
# Planned network (polygons / PDPs / cables / chambers / final trenches /
# ducts incl. drop) + existing brownfield infrastructure.
# ======================================================================

# GPKG files to include in the field-survey package zip.
SURVEY_PACKAGE_FILES = [
    'Objects.gpkg',
    'Polygons.gpkg',
    'PDPs.gpkg',
    'MFG_Service_Areas.gpkg',
    'Feeder_Cable.gpkg',
    'Distribution_Cable.gpkg',
    'Chambers.gpkg',
    # The field team must know which drop legs are on the pole line and why,
    # otherwise an aerial leg looks like a missing trench on the map.
    'Aerial_Drops.gpkg',
    # What the pole / aerial stage actually BUILDS for those legs: the overhead
    # span that is never dug and the cable it carries. Published by the engine
    # and drawn on the map, so a survey package without them is missing part of
    # the network the crew is surveying against.
    'Aerial_Spans.gpkg',
    'Aerial_Cable.gpkg',
    'Final_Trenches.gpkg',
    'Feeder_Ducts.gpkg',
    'Distribution_Ducts.gpkg',
    'Drop_Ducts.gpkg',
    'Coupleurs.gpkg',
    'Existing_Infrastructure.gpkg',
    'Existing_Infrastructure_Points.gpkg',
]

# GeoJSON files to include in the field-survey package zip.
# Maps the engine's output filename → the lowercase filename that will
# appear inside the ZIP (the mobile app's parser is case-insensitive but
# lowercase is conventional).
#
# The HLD engine produces these .geojson files alongside the .gpkg files.
# Coordinates are reprojected from the source CRS (detected from the
# GeoJSON ``crs`` field, defaulting to EPSG:25833) to EPSG:4326 (WGS84)
# so MapLibre can render them correctly.
SURVEY_GEOJSON_FILES = {
    'Objects.geojson': 'objects.geojson',
    'Polygons.geojson': 'polygons.geojson',
    'PDPs.geojson': 'pdps.geojson',
    'Feeder_Cable.geojson': 'feeder_cable.geojson',
    'Distribution_Cable.geojson': 'distribution_cable.geojson',
    'Chambers.geojson': 'chambers.geojson',
    'Aerial_Drops.geojson': 'aerial_drops.geojson',
    'Aerial_Spans.geojson': 'aerial_spans.geojson',
    'Aerial_Cable.geojson': 'aerial_cable.geojson',
    'Trench_Nodes.geojson': 'trench_nodes.geojson',
    'Final_Trenches.geojson': 'final_trenches.geojson',
    'Feeder_Ducts.geojson': 'feeder_ducts.geojson',
    'Distribution_Ducts.geojson': 'distribution_ducts.geojson',
    'Drop_Ducts.geojson': 'drop_ducts.geojson',
    'Coupleurs.geojson': 'coupleurs.geojson',
    'Existing_Infrastructure.geojson': 'existing_infrastructure.geojson',
    'Existing_Infrastructure_Points.geojson': 'existing_infrastructure_points.geojson',
}

# ======================================================================
# HLD DESIGN PACKAGE — every output layer + generated documents.
# This is the full deliverable a design engineer opens in QGIS.
# ======================================================================

# GPKG files to include in the full design package zip.
DESIGN_PACKAGE_FILES = [
    'Objects.gpkg',
    'Polygons.gpkg',
    'PDPs.gpkg',
    'MFG.gpkg',
    'MFG_Service_Areas.gpkg',
    'Final_Trenches.gpkg',
    'Aerial_Drops.gpkg',
    'Aerial_Spans.gpkg',
    'Aerial_Cable.gpkg',
    'Trench_Nodes.gpkg',
    'Pseudo_HH.gpkg',
    'Feeder_Cable.gpkg',
    'Distribution_Cable.gpkg',
    'Feeder_Ducts.gpkg',
    'Distribution_Ducts.gpkg',
    'Drop_Ducts.gpkg',
    'Coupleurs.gpkg',
    'Chambers.gpkg',
    'Poles.gpkg',
    'Existing_Infrastructure.gpkg',
    'Existing_Infrastructure_Points.gpkg',
    'BOQ.xlsx',
    'BOM.xlsx',
]

# GeoJSON files to include in the full design package zip (reprojected to
# WGS84 so they open anywhere, including web viewers).
DESIGN_GEOJSON_FILES = {
    'Objects.geojson': 'objects.geojson',
    'Polygons.geojson': 'polygons.geojson',
    'PDPs.geojson': 'pdps.geojson',
    'MFG.geojson': 'mfg.geojson',
    'MFG_Service_Areas.geojson': 'mfg_service_areas.geojson',
    'Feeder_Cable.geojson': 'feeder_cable.geojson',
    'Distribution_Cable.geojson': 'distribution_cable.geojson',
    'Feeder_Ducts.geojson': 'feeder_ducts.geojson',
    'Distribution_Ducts.geojson': 'distribution_ducts.geojson',
    'Drop_Ducts.geojson': 'drop_ducts.geojson',
    'Coupleurs.geojson': 'coupleurs.geojson',
    'Final_Trenches.geojson': 'final_trenches.geojson',
    'Aerial_Drops.geojson': 'aerial_drops.geojson',
    'Aerial_Spans.geojson': 'aerial_spans.geojson',
    'Aerial_Cable.geojson': 'aerial_cable.geojson',
    'Trench_Nodes.geojson': 'trench_nodes.geojson',
    'Chambers.geojson': 'chambers.geojson',
    'Poles.geojson': 'poles.geojson',
    'Existing_Infrastructure.geojson': 'existing_infrastructure.geojson',
    'Existing_Infrastructure_Points.geojson': 'existing_infrastructure_points.geojson',
}

# Fallback CRS when the GeoJSON has no ``crs`` field.
# EPSG:25833 = UTM Zone 33N (used for Berlin / central Europe test data).
# This should match the CRS of the input road network.
DEFAULT_SOURCE_CRS = 'EPSG:25833'
