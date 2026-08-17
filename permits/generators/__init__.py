"""Permit package generators (Phase 2).

Produces the LLD-stage permit package from the persisted LLD layers +
permit matrix, per ``docs/subprojects/permit-engine/DESIGN.md`` §6 (LLD —
package):

* route drawings (GeoJSON extracts per layer)
* trench cross-sections (SVG per trench type)
* chamber / handhole schedules
* cabinet (PDP) schedules
* HDD crossing drawings (where railway/waterway crossings exist)
* utility conflict report (brownfield coexistence)
* traffic management plan (rules-driven from road class / surface / length)
* surface restoration plan
* BOQ reference
* auto-populated permit application forms

Generators are pure functions of the project's persisted data; the
``package.py`` orchestrator bundles the outputs into a versioned zip and
promotes document evidence on the permit matrix (readiness checker).
"""
