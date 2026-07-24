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
FTTH_ENGINE_URL = os.getenv(
    "FTTH_ENGINE_URL",
    "http://ftth.zeabur.app",
).rstrip("/")

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
     "outputs": ["Feeder_Ducts.gpkg", "Distribution_Ducts.gpkg"]},
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

# List of output files to include in the survey package zip
SURVEY_PACKAGE_FILES = [
    "Objects.gpkg", "Polygons.gpkg", "PDPs.gpkg", "MFG.gpkg",
    "Feeder_Trench.gpkg", "Distribution_Trench.gpkg", "Garden_Trench.gpkg",
    "Drill_Trench.gpkg", "Final_Trenches.gpkg",
    "Feeder_Cable.gpkg", "Distribution_Cable.gpkg",
    "Feeder_Ducts.gpkg", "Distribution_Ducts.gpkg",
    "BOQ.xlsx", "BOM.xlsx",
]

# GeoJSON files to include in the survey package zip.
# Maps the engine's output filename → the lowercase filename that will
# appear inside the ZIP (the mobile app's parser is case-insensitive but
# lowercase is conventional).
#
# The HLD engine produces these .geojson files alongside the .gpkg files.
# Coordinates are reprojected from the source CRS (detected from the
# GeoJSON ``crs`` field, defaulting to EPSG:25833) to EPSG:4326 (WGS84)
# so MapLibre can render them correctly.
SURVEY_GEOJSON_FILES = {
    "Objects.geojson":              "objects.geojson",
    "Polygons.geojson":             "polygons.geojson",
    "PDPs.geojson":                 "pdps.geojson",
    "MFG.geojson":                  "mfg.geojson",
    "Feeder_Cable.geojson":         "feeder_cable.geojson",
    "Distribution_Cable.geojson":   "distribution_cable.geojson",
    "Feeder_Ducts.geojson":         "feeder_ducts.geojson",
    "Distribution_Ducts.geojson":   "distribution_ducts.geojson",
    "Feeder_Trench.geojson":        "feeder_trench.geojson",
    "Distribution_Trench.geojson":  "distribution_trench.geojson",
    "Garden_Trench.geojson":        "garden_trench.geojson",
    "Final_Trenches.geojson":       "final_trenches.geojson",
}

# Fallback CRS when the GeoJSON has no ``crs`` field.
# EPSG:25833 = UTM Zone 33N (used for Berlin / central Europe test data).
# This should match the CRS of the input road network.
DEFAULT_SOURCE_CRS = "EPSG:25833"
