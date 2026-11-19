"""Shared field-spec helpers for the per-layer editor and scoped templates.

The two admin surfaces write the same shape: ``role``/``widget``/``unit`` for
the clients, plus ``key``/``label``/``order`` for display. Keeping the merge
here means a template can never produce a spec the per-layer editor would not.
"""

from __future__ import annotations

from projects.api.layer_field_config import TYPE_MAP


def field_from_type(key: str, label: str, type_key: str, order: int) -> dict:
    """One admin-added field, in the shape both clients render."""
    chosen = type_key if type_key in TYPE_MAP else 'number'
    spec = TYPE_MAP[chosen]
    return {
        'key': key,
        'label': label or key,
        'role': spec['role'],
        'widget': spec['widget'],
        'unit': spec['unit'],
        'editable': True,
        'type': chosen,
        'order': order,
    }


def _type_of(item: dict) -> str:
    """The editor type behind a stored spec (role/widget), when it has no type."""
    explicit = item.get('type')
    if explicit in {'number', 'text', 'location', 'textarea'}:
        return explicit
    if item.get('role') == 'measurement' or item.get('widget') == 'number':
        return 'number'
    if item.get('role') == 'note' or item.get('widget') == 'textarea':
        return 'textarea'
    if item.get('role') == 'measurement_text':
        return 'location'
    return 'text'


def merge_extra_fields(base, extra) -> list[dict]:
    """Append template fields to a layer's own schema.

    Append-only, and a key the layer already carries is skipped: a global
    template cannot silently change what an existing measurement means in a
    project it was never previewed against. Changing an existing field stays a
    deliberate act in Layer Details → Field Configuration.
    """
    merged = [dict(field) for field in base or [] if isinstance(field, dict)]
    seen = {field.get('key') for field in merged}
    next_order = max((int(field.get('order') or 0) for field in merged), default=-1) + 1

    for item in extra or []:
        if not isinstance(item, dict):
            continue
        key = item.get('key')
        if not key or key in seen:
            continue
        merged.append(
            field_from_type(key, item.get('label') or key, _type_of(item), next_order)
        )
        seen.add(key)
        next_order += 1

    return merged
