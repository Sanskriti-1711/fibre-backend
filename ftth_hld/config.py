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
# The engine (ftth-engine/) handles Docker exec/cp for qgis_process.
#
# Resolution order:
#   1. FTTH_ENGINE_URL env var (always wins — set this on Zeabur if the
#      production engine moves).
#   2. Local development (FTTH_DB=local|dev|docker — same flag settings.py
#      uses) → http://localhost:8080 (the FastAPI engine started from
#      HLD_Planning_01/web/backend).
#   3. Production default → https://ftth.zeabur.app (live engine built from
#      the sanskriti17/ftth_planning Docker image).
def _default_engine_url() -> str:
    if os.getenv("FTTH_DB", "").lower() in ("local", "dev", "docker"):
        return "http://localhost:8080"
    return "https://ftth.zeabur.app"


FTTH_ENGINE_URL = os.getenv("FTTH_ENGINE_URL", _default_engine_url()).rstrip("/")

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
    {"index": 0, "name": "Object Layer",    "algorithm": "01_object_layer",
     "outputs": ["Objects"]},
    {"index": 1, "name": "Polygon Layer",   "algorithm": "02_polygon_layer",
     "outputs": ["Polygons"]},
    {"index": 2, "name": "Network Layer",   "algorithm": "03_network_layer",
     "outputs": ["PDPs", "MFG"]},
    {"index": 3, "name": "Trench Layer",    "algorithm": "04_trench_layer",
     "outputs": ["Feeder_Trench", "Distribution_Trench",
                  "Garden_Trench", "Drill_Trench", "Final_Trenches"]},
    {"index": 4, "name": "Cable Layer",     "algorithm": "06_cable_layer",
     "outputs": ["Feeder_Cable", "Distribution_Cable"]},
    {"index": 5, "name": "Duct Layer",      "algorithm": "05_duct_layer",
     "outputs": ["Feeder_Ducts", "Distribution_Ducts"]},
]

# Maps API-friendly layer name → (GPKG stem, internal layer name).
# Individual sub-layers (feeder_cable, distribution_ducts, etc.) each get
# their own entry so the frontend can fetch them independently by name.
LAYER_NAME_MAP = {
    # Canonical names matching the backend ONECLICK_OUTPUTS
    "objects":             ("Objects",              "object_layer"),
    "polygons":            ("Polygons",             "polygon_layer"),
    "pdps":                ("PDPs",                 "PDPs"),
    "mfg":                 ("MFG",                  "MFG"),
    "feeder_cable":        ("Feeder_Cable",         "Feeder_Cable"),
    "distribution_cable":  ("Distribution_Cable",   "Distribution_Cable"),
    "feeder_ducts":        ("Feeder_Ducts",         "Feeder_Ducts"),
    "distribution_ducts":  ("Distribution_Ducts",   "Distribution_Ducts"),
    "drop_ducts":          ("Drop_Ducts",             "Drop_Ducts"),
    "chambers":            ("Chambers",             "Chambers"),
    "poles":               ("Poles",                "Poles"),
    "brownfield":          ("Existing_Infrastructure", "brownfield"),
    "trenches":            ("Final_Trenches",       "trench_layer"),
    # Backward-compatible aliases
    "network":             ("Network",              "network_layer"),
    "cables":              ("Feeder_Cable",         "cable_layer"),
    "ducts":               ("Feeder_Ducts",         "duct_layer"),
}

# Pipeline steps for step-by-step execution (matches HLD_Planning_01 ONECLICK_OUTPUTS)
PIPELINE_STEPS = [
    {"name": "object",  "alg_id": "hldplanning:01_object_layer",  "label": "Object Layer",
     "outputs": ["Objects.gpkg"]},
    {"name": "polygon", "alg_id": "hldplanning:02_polygon_layer", "label": "Polygon Layer",
     "outputs": ["Polygons.gpkg"]},
    {"name": "network", "alg_id": "hldplanning:03_network_layer", "label": "Network Layer",
     "outputs": ["PDPs.gpkg", "MFG.gpkg"]},
    {"name": "trench",  "alg_id": "hldplanning:04_trench_layer",  "label": "Trench Layer",
     "outputs": ["Feeder_Trench.gpkg", "Distribution_Trench.gpkg",
                  "Garden_Trench.gpkg", "Drill_Trench.gpkg", "Final_Trenches.gpkg"]},
    {"name": "cable",   "alg_id": "hldplanning:06_cable_layer",   "label": "Cable Layer",
     "outputs": ["Feeder_Cable.gpkg", "Distribution_Cable.gpkg"]},
    {"name": "duct",    "alg_id": "hldplanning:05_duct_layer",    "label": "Duct Layer",
     "outputs": ["Feeder_Ducts.gpkg", "Distribution_Ducts.gpkg", "Drop_Ducts.gpkg"]},
]

# Step dependency chain: which step must be completed before this one
STEP_DEPENDENCIES = {
    "object": None,
    "polygon": "object",
    "network": "polygon",
    "trench": "network",
    "cable": "trench",
    "duct": "trench",
}

# ======================================================================
# FIELD SURVEY PACKAGE — compact subset a surveyor needs in the field.
# Planned network (polygons / PDPs / cables / chambers / final trenches /
# ducts incl. drop) + existing brownfield infrastructure.
# ======================================================================

# GPKG files to include in the field-survey package zip.
SURVEY_PACKAGE_FILES = [
    "Polygons.gpkg", "PDPs.gpkg",
    "Feeder_Cable.gpkg", "Distribution_Cable.gpkg",
    "Chambers.gpkg",
    "Final_Trenches.gpkg",
    "Feeder_Ducts.gpkg", "Distribution_Ducts.gpkg", "Drop_Ducts.gpkg",
    "Existing_Infrastructure.gpkg", "Existing_Infrastructure_Points.gpkg",
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
    "Polygons.geojson":             "polygons.geojson",
    "PDPs.geojson":                 "pdps.geojson",
    "Feeder_Cable.geojson":         "feeder_cable.geojson",
    "Distribution_Cable.geojson":   "distribution_cable.geojson",
    "Chambers.geojson":             "chambers.geojson",
    "Final_Trenches.geojson":       "final_trenches.geojson",
    "Feeder_Ducts.geojson":         "feeder_ducts.geojson",
    "Distribution_Ducts.geojson":   "distribution_ducts.geojson",
    "Drop_Ducts.geojson":           "drop_ducts.geojson",
    "Existing_Infrastructure.geojson":       "existing_infrastructure.geojson",
    "Existing_Infrastructure_Points.geojson": "existing_infrastructure_points.geojson",
}

# ======================================================================
# HLD DESIGN PACKAGE — every output layer + generated documents.
# This is the full deliverable a design engineer opens in QGIS.
# ======================================================================

# GPKG files to include in the full design package zip.
DESIGN_PACKAGE_FILES = [
    "Objects.gpkg", "Polygons.gpkg", "PDPs.gpkg", "MFG.gpkg",
    "Feeder_Trench.gpkg", "Distribution_Trench.gpkg", "Garden_Trench.gpkg",
    "Drill_Trench.gpkg", "Final_Trenches.gpkg", "Pseudo_HH.gpkg",
    "Feeder_Cable.gpkg", "Distribution_Cable.gpkg",
    "Feeder_Ducts.gpkg", "Distribution_Ducts.gpkg", "Drop_Ducts.gpkg",
    "Chambers.gpkg", "Poles.gpkg",
    "Existing_Infrastructure.gpkg", "Existing_Infrastructure_Points.gpkg",
    "BOQ.xlsx", "BOM.xlsx",
]

# GeoJSON files to include in the full design package zip (reprojected to
# WGS84 so they open anywhere, including web viewers).
DESIGN_GEOJSON_FILES = {
    "Objects.geojson":              "objects.geojson",
    "Polygons.geojson":             "polygons.geojson",
    "PDPs.geojson":                 "pdps.geojson",
    "MFG.geojson":                  "mfg.geojson",
    "Feeder_Cable.geojson":         "feeder_cable.geojson",
    "Distribution_Cable.geojson":   "distribution_cable.geojson",
    "Feeder_Ducts.geojson":         "feeder_ducts.geojson",
    "Distribution_Ducts.geojson":   "distribution_ducts.geojson",
    "Drop_Ducts.geojson":           "drop_ducts.geojson",
    "Feeder_Trench.geojson":        "feeder_trench.geojson",
    "Distribution_Trench.geojson":  "distribution_trench.geojson",
    "Garden_Trench.geojson":        "garden_trench.geojson",
    "Final_Trenches.geojson":       "final_trenches.geojson",
    "Chambers.geojson":             "chambers.geojson",
    "Poles.geojson":                "poles.geojson",
    "Existing_Infrastructure.geojson":       "existing_infrastructure.geojson",
    "Existing_Infrastructure_Points.geojson": "existing_infrastructure_points.geojson",
}

# Fallback CRS when the GeoJSON has no ``crs`` field.
# EPSG:25833 = UTM Zone 33N (used for Berlin / central Europe test data).
# This should match the CRS of the input road network.
DEFAULT_SOURCE_CRS = "EPSG:25833"
