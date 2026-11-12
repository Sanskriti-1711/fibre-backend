"""The engineer form schema derived from a layer's own attributes.

Both entry points into a supervised layer — the LLD handoff and a GeoPackage
import — have to answer the same question: which of these attributes does the
field engineer measure, and with what input? The rule lives here so the two
cannot drift apart, and so a layer with no admin :class:`LayerFieldConfig` still
gives its engineers a usable form instead of an empty one.

The heuristic is deliberately conservative: an attribute is a measurement when
its name says so (``DEPTH_MM``, ``length_m``) or its value is a number, and it
is not an identifier. Identity and foreign-key columns (``cable_id``,
``TRUNK_NO``, ``PARENT_TRENCH``) stay read-only reference data even when their
values are numbers: editing one changes what the feature *is*, not what was
built, and the as-built comparison would then disagree with the design for the
wrong reason. Free-text attributes are reference data too. The engineer can
still add their own fields from the field form, and an admin can flip any of
these in Layer Details → Field Configuration.
"""

from __future__ import annotations

_IDENTITY_KEYS = {'id', 'gid', 'fid', 'objectid', 'feature_id', 'uuid'}

# Suffixes and prefixes that mark a column as pointing at other data rather than
# describing the build. Matched on the key, because an identifier's value is very
# often a number and a number is otherwise our whole measurement signal.
_REFERENCE_SUFFIXES = (
    '_id',
    '_ids',
    '_no',
    '_num',
    '_code',
    '_ref',
    '_key',
    '_name',
    '_uuid',
    '_gid',
    '_fid',
)

# ``PARENT_TRENCH: "1"`` names its target in the prefix instead.
_REFERENCE_PREFIXES = ('parent_',)

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
    if isinstance(value, int | float):
        return True
    if isinstance(value, str) and value.strip():
        try:
            float(value)
        except ValueError:
            return False
        return True
    return False


def _is_reference(key):
    """True for columns that identify this feature or point at another one.

    ``cable_id``, ``TRUNK_NO`` and ``PARENT_TRENCH`` all arrive with numeric
    values, so the value test alone would offer them as editable number boxes on
    the engineer's form.
    """
    return (
        key in _IDENTITY_KEYS
        or key.endswith(_REFERENCE_SUFFIXES)
        or key.startswith(_REFERENCE_PREFIXES)
    )


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

        numeric = _is_number(value)
        measurement = not _is_reference(lowered) and (
            any(hint in lowered for hint in _MEASUREMENT_HINTS) or numeric
        )

        if measurement:
            # A hinted field whose real value is text (``SIZE: "600x600 mm"``,
            # ``"Large (1500×1200 mm)"``) cannot go through a number input: it
            # stays editable, as a text box with no unit.
            schema.append(
                {
                    'key': key_text,
                    'label': label,
                    'role': 'measurement' if numeric else 'measurement_text',
                    'widget': 'number' if numeric else 'text',
                    'unit': numeric,
                    'editable': True,
                    'type': 'number' if numeric else 'text',
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
