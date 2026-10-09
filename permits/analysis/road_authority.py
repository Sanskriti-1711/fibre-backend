"""Road-authority mapping (Straßenbaulastträger) from OSM road classes.

Deterministic mapping from the OSM ``fclass`` value persisted on HLD trench
segments to the responsible German road authority. The current seed covers
the German case (Berlin focus); the mapping is a plain dict so other
countries can be added without an engine change.

Authority codes below are resolved against ``PermitAuthority.code`` — see
``management/commands/seed_permit_authorities.py``.
"""

from __future__ import annotations

# OSM fclass -> (authority code, owner label, note)
#
# German hierarchy (Straßenbaulastträger):
#   Bundesautobahn / Bundesstraße -> Bund (Autobahn GmbH / Bundesstraßenverwaltung)
#   Landesstraße (L)              -> Land (SenMVKU in Berlin)
#   Kreisstraße (K)               -> Landkreis (Berlin has none — tertiary is Land)
#   Gemeindestraße                -> Gemeinde / Bezirk (Bezirksamt in Berlin)
#   Fuß-/Radweg                   -> Gemeinde / Bezirk
#   private / service / track     -> private owner (no road-authority permit)
ROAD_CLASS_MAP: dict[str, tuple[str, str, str]] = {
    # ── Bundesautobahn / Bundesstraße -> Bund ────────────────────────────
    'motorway': ('DE-ROAD-BUND', 'Autobahn GmbH des Bundes (Bund)', 'Bundesautobahn'),
    'motorway_link': ('DE-ROAD-BUND', 'Autobahn GmbH des Bundes (Bund)', 'Bundesautobahn (ramp)'),
    'trunk': ('DE-ROAD-BUND', 'Bundesstraßenverwaltung (Bund)', 'Bundesstraße (trunk)'),
    'trunk_link': ('DE-ROAD-BUND', 'Bundesstraßenverwaltung (Bund)', 'Bundesstraße (ramp)'),
    'primary': ('DE-ROAD-BUND', 'Bundesstraßenverwaltung (Bund)', 'Bundesstraße'),
    'primary_link': ('DE-ROAD-BUND', 'Bundesstraßenverwaltung (Bund)', 'Bundesstraße (ramp)'),
    # ── Landesstraße -> Land (Berlin: SenMVKU) ───────────────────────────
    'secondary': ('DE-ROAD-LAND-BE', 'Land Berlin — SenMVKU (Landesstraße)', 'Landesstraße'),
    'secondary_link': (
        'DE-ROAD-LAND-BE',
        'Land Berlin — SenMVKU (Landesstraße)',
        'Landesstraße (ramp)',
    ),
    'tertiary': (
        'DE-ROAD-LAND-BE',
        'Land Berlin — SenMVKU (Landesstraße)',
        'Landesstraße / Kreisstraße',
    ),
    'tertiary_link': (
        'DE-ROAD-LAND-BE',
        'Land Berlin — SenMVKU (Landesstraße)',
        'Landesstraße (ramp)',
    ),
    # ── Gemeindestraße / Fußweg / Radweg -> Bezirk ───────────────────────
    'unclassified': ('DE-ROAD-BEZIRK-BE', 'Bezirksamt Berlin (Gemeindestraße)', 'Gemeindestraße'),
    'residential': (
        'DE-ROAD-BEZIRK-BE',
        'Bezirksamt Berlin (Gemeindestraße)',
        'Gemeindestraße (residential)',
    ),
    'living_street': (
        'DE-ROAD-BEZIRK-BE',
        'Bezirksamt Berlin (Gemeindestraße)',
        'Verkehrsberuhigter Bereich',
    ),
    'footway': ('DE-ROAD-BEZIRK-BE', 'Bezirksamt Berlin (Gehweg)', 'Fußweg'),
    'path': ('DE-ROAD-BEZIRK-BE', 'Bezirksamt Berlin (Geh-/Radweg)', 'Fuß-/Radweg'),
    'cycleway': ('DE-ROAD-BEZIRK-BE', 'Bezirksamt Berlin (Radweg)', 'Radweg'),
    'pedestrian': ('DE-ROAD-BEZIRK-BE', 'Bezirksamt Berlin (Fußgängerzone)', 'Fußgängerzone'),
    'steps': ('DE-ROAD-BEZIRK-BE', 'Bezirksamt Berlin (Treppe)', 'Treppenanlage'),
    'bridleway': ('DE-ROAD-BEZIRK-BE', 'Bezirksamt Berlin (Reitweg)', 'Reitweg'),
    # ── private / service / track -> private owner ──────────────────────
    'service': ('DE-ROAD-PRIVATE', 'Private road owner', 'Privatstraße / Erschließung'),
    'track': ('DE-ROAD-PRIVATE', 'Private road owner', 'Wirtschaftsweg (privat)'),
}

# Fallback for unmapped classes — recorded honestly, never assumed.
FALLBACK_CODE = 'DE-ROAD-UNKNOWN'
FALLBACK_LABEL = 'Road authority (to be resolved from road class)'


def resolve_road_authority(fclass: str) -> tuple[str, str]:
    """Map an OSM ``fclass`` to (authority code, owner label).

    Unknown/missing classes fall back to ``DE-ROAD-UNKNOWN`` so the row is
    recorded as unresolved rather than silently assigned to the wrong
    authority.
    """
    key = (fclass or '').strip().lower()
    if not key:
        return FALLBACK_CODE, FALLBACK_LABEL
    entry = ROAD_CLASS_MAP.get(key)
    if entry is None:
        return FALLBACK_CODE, FALLBACK_LABEL
    return entry[0], entry[1]
