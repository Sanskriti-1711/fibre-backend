"""Planned vs as-built deviation (INTEGRATION_PLAN.md §3 Stage 5.2).

A supervision feature already carries both sides of the comparison: the
design's values live in ``properties`` (copied by the LLD handoff), and the
engineer's readings live in ``field_measurements`` as ``{key: {value, unit}}``.

This module turns those two dictionaries into rows a reviewer can read —
planned, as built, the difference, and whether it is inside the layer's
tolerance — instead of making every client repeat the arithmetic. It computes;
it never writes, so it cannot change what either side recorded.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation


def as_number(value):
    """Numeric view of a design attribute or measurement, or ``None``.

    Strings are accepted because imported attributes are often text ("18.86")
    and a form submits strings. Anything that is not a number returns ``None``
    rather than zero: treating a missing reading as 0 would invent a deviation
    of the full planned value.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | float):
        return Decimal(str(value))
    if isinstance(value, str):
        text = value.strip().replace(',', '')
        if not text:
            return None
        try:
            return Decimal(text)
        except InvalidOperation:
            return None
    return None


def _measurement(entry):
    """The value and unit of one ``field_measurements`` entry.

    Accepts the documented ``{value, unit}`` shape and a bare scalar, because
    older rows and hand-made API calls both exist.
    """
    if isinstance(entry, dict):
        return entry.get('value'), entry.get('unit') or ''
    return entry, ''


def feature_deviation(feature, tolerance=0):
    """Compare one feature's readings against its planned values.

    Only keys that exist on both sides are compared — a reading for an
    attribute the design never recorded has no planned value to deviate from,
    and the layer's own tolerance decides what counts as "as designed".
    """
    properties = feature.properties if isinstance(feature.properties, dict) else {}
    measurements = (
        feature.field_measurements if isinstance(feature.field_measurements, dict) else {}
    )
    limit = as_number(tolerance) or Decimal('0')

    fields = []
    for key, entry in measurements.items():
        if key not in properties:
            continue
        planned = as_number(properties.get(key))
        raw_value, unit = _measurement(entry)
        as_built = as_number(raw_value)
        if planned is None or as_built is None:
            continue
        delta = planned - as_built
        fields.append(
            {
                'key': key,
                'planned': float(planned),
                'as_built': float(as_built),
                'delta': float(delta),
                'unit': unit,
                'within_tolerance': abs(delta) <= limit,
            }
        )

    outside = [row for row in fields if not row['within_tolerance']]
    return {
        'tolerance': float(limit),
        'measured': len(fields),
        'outside_tolerance': len(outside),
        'has_deviation': bool(outside),
        'fields': fields,
    }
