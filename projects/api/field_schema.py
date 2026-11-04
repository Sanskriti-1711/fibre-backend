"""The engineer form schema derived from a layer's own attributes.

Both entry points into a supervised layer — the LLD handoff and a GeoPackage
import — have to answer the same question: which of these attributes does the
field engineer measure, and with what input? The rule lives here so the two
cannot drift apart, and so a layer with no admin :class:`LayerFieldConfig` still
gives its engineers a usable form instead of an empty one.

The heuristic is deliberately conservative: an attribute is a measurement when
its name says so (``DEPTH_MM``, ``length_m``) or its value is a number. Identity
columns and free-text attributes stay read-only reference data — the engineer
can still add their own fields from the field form, and an admin can flip any
of these in Layer Details → Field Configuration.
"""

from __future__ import annotations

_IDENTITY_KEYS = {'id', 'gid', 'fid', 'objectid', 'feature_id', 'uuid'}

_MEASUREMENT_HINTS = (
    'depth',
    'width',
    'height',
    'length',
    'diameter',
    'radius',
    'size',
    'count',
    'qty',
    'quantity',
    'elevation',
    'offset',
    'area',
    'volume',
    'pressure',
    'tension',
    'slope',
    'grade',
    'spacing',
    'clearance',
    'gauge',
    'thickness',
    'capacity',
)


def _is_number(value):
    """True for numbers and numeric strings — the shape measurements arrive in."""
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str) and value.strip():
        try:
            float(value)
        except ValueError:
            return False
        return True
    return False


def derive_field_schema(properties):
    """Return ``[{key,label,role,widget,unit,editable,type,order}]`` or ``None``.

    The shape is the one the admin API accepts, so an auto-derived schema can be
    saved unchanged from the Field Configuration screen.
    """
    if not isinstance(properties, dict) or not properties:
        return None

    schema = []
    for order, (key, value) in enumerate(properties.items()):
        key_text = str(key)
        lowered = key_text.lower()
        label = key_text.replace('_', ' ').title()

        measurement = (
            lowered not in _IDENTITY_KEYS
            and (any(hint in lowered for hint in _MEASUREMENT_HINTS) or _is_number(value))
        )

        if measurement:
            schema.append(
                {
                    'key': key_text,
                    'label': label,
                    'role': 'measurement',
                    'widget': 'number',
                    'unit': True,
                    'editable': True,
                    'type': 'number',
                    'order': order,
                }
            )
        else:
            schema.append(
                {
                    'key': key_text,
                    'label': label,
                    'role': 'reference',
                    'widget': 'readonly',
                    'unit': False,
                    'editable': False,
                    'type': 'text',
                    'order': order,
                }
            )

    return schema


def has_editable(schema):
    """True when a stored schema already asks the engineer for something."""
    return any(isinstance(item, dict) and item.get('editable') for item in schema or [])
