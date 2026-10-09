"""
Survey field auto-fill from photo context (Tier-1 A3).

Maps classified photo tags (A1) + capture context onto suggested survey
field values. Returns suggestions only — the engineer confirms them in the
survey form, so a mis-tag never silently corrupts the data.

Rules are ordered most-specific first; the first match per field wins.
"""

from __future__ import annotations

# Tag → field suggestions. Values match the TrenchSurvey choice enums.
_TAG_RULES = [
    # A photo showing a road implies a road crossing on trench work.
    ({'road'}, {'road_crossing': True, 'traffic_sensitive': True}, 0.8),
    # Rail / water tags imply the corresponding crossings.
    ({'rail'}, {'rail_crossing': True, 'permit_required': True}, 0.9),
    ({'water'}, {'river_crossing': True, 'permit_required': True}, 0.85),
    # Footpath implies a footpath crossing.
    ({'footpath'}, {'footpath_crossing': True}, 0.7),
    # Aerial context suggests overhead construction.
    ({'aerial', 'pole'}, {'construction_method': 'aerial'}, 0.75),
    # Grass context suggests open cut on grass.
    ({'grass'}, {'surface_type': 'grass'}, 0.7),
    ({'road'}, {'surface_type': 'asphalt'}, 0.65),
]


def autofill_from_tags(tags: list[str], sf=None) -> dict:
    """Compute suggested field values from classified tags.

    Returns {field: {value, confidence, source_tag}} — empty when no rule
    matches. Never raises.
    """
    suggestions: dict = {}
    if not tags:
        return suggestions

    tag_set = set(tags)
    for rule_tags, fields, confidence in _TAG_RULES:
        if not (tag_set & rule_tags):
            continue
        source_tag = sorted(tag_set & rule_tags)[0]
        for field, value in fields.items():
            if field in suggestions:
                continue  # first (most specific) match wins
            # Layer-aware guard: crossing flags only apply to line layers.
            if field.endswith('_crossing') and not _is_line_layer(sf):
                continue
            # Aerial construction only makes sense for drop/trench layers.
            if field == 'construction_method' and value == 'aerial' and not _allows_aerial(sf):
                continue
            suggestions[field] = {
                'value': value,
                'confidence': confidence,
                'source_tag': source_tag,
            }
    return suggestions


def _is_line_layer(sf) -> bool:
    if sf is None:
        return True  # unknown layer — don't block crossing suggestions
    name = (
        (getattr(sf, 'layer_name', '') or '') + ' ' + (getattr(sf, 'layer_id', '') or '')
    ).lower()
    return any(k in name for k in ('trench', 'duct', 'cable', 'line'))


def _allows_aerial(sf) -> bool:
    name = (
        (getattr(sf, 'layer_name', '') or '') + ' ' + (getattr(sf, 'layer_id', '') or '')
    ).lower()
    # Aerial is drop-only per the planning document; feeder/distribution
    # stay underground.
    return 'drop' in name or 'garden' in name
