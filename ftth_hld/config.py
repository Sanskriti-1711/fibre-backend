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
#   1. FTTH_ENGINE_URL env var (always wins — set this on Zeabur if the
#      production engine moves).
#   2. Local development (FTTH_DB=local|dev|docker — same flag settings.py
#      uses) → http://127.0.0.1:8080 (the FastAPI engine started from
#      HLD_Planning_01/web/backend).
#      NOTE: 127.0.0.1 is used instead of localhost on purpose — on Windows
#      ``localhost`` resolves to IPv6 ::1 first, and every HTTP request burns
#      ~2s waiting for the IPv6 connect to fail before falling back to IPv4.
#      With ~40 file fetches per design-package download that added ~80s.
#   3. Production default → https://ftth.zeabur.app (live engine built from
#      the sanskriti17/ftth_planning Docker image).
def _default_engine_url() -> str:
    if os.getenv("FTTH_DB", "").lower() in ("local", "dev", "docker"):
        return "http://127.0.0.1:8080"
    # Local development (DEBUG on) defaults to the local FastAPI engine so the
    # results map works even when the server is started without FTTH_ENGINE_URL.
    try:
        from django.conf import settings
        if getattr(settings, "DEBUG", False):
            return "http://127.0.0.1:8080"
    except Exception:
        pass
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
     "outputs": ["Final_Trenches"]},
    # Phase C cascade (TRENCH_DESIGN.md §6.1): ducts are laid before the
    # cables are pulled, so the duct stage carries the lower index.
    {"index": 4, "name": "Duct Layer",      "algorithm": "05_duct_layer",
     "outputs": ["Feeder_Ducts", "Distribution_Ducts"]},
    {"index": 5, "name": "Cable Layer",     "algorithm": "06_cable_layer",
     "outputs": ["Feeder_Cable", "Distribution_Cable"]},
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
    "coupleurs":           ("Coupleurs",              "Coupleurs"),
    "chambers":            ("Chambers",             "Chambers"),
    "poles":               ("Poles",                "Poles"),
    # Aerial legs the trench stage classified (never excavated): a design
    # decision, separate from the aerial drop the pole/aerial stage builds.
    "aerial_drops":        ("Aerial_Drops",         "aerial_drops"),
    # What the pole / aerial-drop stage BUILDS for those classified legs: the
    # drop trench (a span on a pole, never dug) and the cable it carries. Both
    # are published by the engine and ingested into `gis.aerial_drop_trench_layer`
    # / `gis.aerial_cable_layer`, but they were missing from this map — and
    # `LayerGeoJSONView` rejects any name that is not in it, so the two layers
    # 404'd on the platform and could never be drawn, in any project. That is
    # why the aerial routes were invisible on the map even on a run with a real
    # aerial classification.
    "aerial_drop_trenches": ("Aerial_Drop_Trenches", "aerial_drop_trenches"),
    "aerial_cable":        ("Aerial_Cable",         "aerial_cable"),
    # The trench designer's structural nodes (HDD pits / junctions / PDPs /
    # bends / pulls): the evidence behind every planned chamber, and the layer
    # the map draws the structures' real positions from.
    "trench_nodes":        ("Trench_Nodes",         "trench_nodes"),
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
     "outputs": ["Final_Trenches.gpkg"]},
    {"name": "cable",   "alg_id": "hldplanning:06_cable_layer",   "label": "Cable Layer",
     "outputs": ["Feeder_Cable.gpkg", "Distribution_Cable.gpkg"]},
    {"name": "duct",    "alg_id": "hldplanning:05_duct_layer",    "label": "Duct Layer",
     "outputs": ["Feeder_Ducts.gpkg", "Distribution_Ducts.gpkg", "Drop_Ducts.gpkg", "Coupleurs.gpkg"]},
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
    "Objects.gpkg",
    "Polygons.gpkg", "PDPs.gpkg",
    "Feeder_Cable.gpkg", "Distribution_Cable.gpkg",
    "Chambers.gpkg",
    # The field team must know which drop legs are on the pole line and why,
    # otherwise an aerial leg looks like a missing trench on the map.
    "Aerial_Drops.gpkg",
    "Final_Trenches.gpkg",
    "Feeder_Ducts.gpkg", "Distribution_Ducts.gpkg", "Drop_Ducts.gpkg",
    "Coupleurs.gpkg",
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
    "Objects.geojson":              "objects.geojson",
    "Polygons.geojson":             "polygons.geojson",
    "PDPs.geojson":                 "pdps.geojson",
    "Feeder_Cable.geojson":         "feeder_cable.geojson",
    "Distribution_Cable.geojson":   "distribution_cable.geojson",
    "Chambers.geojson":             "chambers.geojson",
    "Aerial_Drops.geojson":         "aerial_drops.geojson",
    "Trench_Nodes.geojson":         "trench_nodes.geojson",
    "Final_Trenches.geojson":       "final_trenches.geojson",
    "Feeder_Ducts.geojson":         "feeder_ducts.geojson",
    "Distribution_Ducts.geojson":   "distribution_ducts.geojson",
    "Drop_Ducts.geojson":           "drop_ducts.geojson",
    "Coupleurs.geojson":            "coupleurs.geojson",
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
    "Final_Trenches.gpkg", "Aerial_Drops.gpkg", "Trench_Nodes.gpkg", "Pseudo_HH.gpkg",
    "Feeder_Cable.gpkg", "Distribution_Cable.gpkg",
    "Feeder_Ducts.gpkg", "Distribution_Ducts.gpkg", "Drop_Ducts.gpkg",
    "Coupleurs.gpkg",
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
    "Coupleurs.geojson":            "coupleurs.geojson",
    "Final_Trenches.geojson":       "final_trenches.geojson",
    "Aerial_Drops.geojson":         "aerial_drops.geojson",
    "Trench_Nodes.geojson":         "trench_nodes.geojson",
    "Chambers.geojson":             "chambers.geojson",
    "Poles.geojson":                "poles.geojson",
    "Existing_Infrastructure.geojson":       "existing_infrastructure.geojson",
    "Existing_Infrastructure_Points.geojson": "existing_infrastructure_points.geojson",
}

# Fallback CRS when the GeoJSON has no ``crs`` field.
# EPSG:25833 = UTM Zone 33N (used for Berlin / central Europe test data).
# This should match the CRS of the input road network.
DEFAULT_SOURCE_CRS = "EPSG:25833"
