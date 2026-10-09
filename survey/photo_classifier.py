"""
Photo classification (Tier-1 A1).

Auto-tags uploaded survey photos with what they show: trench, cable, pole,
chamber, pdp, premise, crossing, obstruction, plus context tags (road,
grass, footpath) used by the A3 field auto-fill rules.

Two backends, chosen by settings.PHOTO_CLASSIFIER:

  "heuristic" (default) — zero-dependency tagger that reads capture metadata
  (GPS, time, description, layer) plus basic EXIF. Deterministic and fast.

  "ml" — pluggable on-device/server CNN (MobileNet/EfficientNet). Implement
  ``MLClassifyFn`` (bytes -> {label: confidence}) and set
  PHOTO_CLASSIFIER_ML_FN to a dotted import path. Falls back to heuristic
  when the backend errors or returns nothing.

Tags are stored on SurveyFeature.photo_tags and FieldEvidence.description
prefix, and drive the A3 auto-fill suggestions in the survey app.
"""

from __future__ import annotations

from django.conf import settings

# Tags the system understands (A3 rules key off these).
KNOWN_TAGS = {
    'trench',
    'cable',
    'pole',
    'chamber',
    'pdp',
    'premise',
    'crossing',
    'obstruction',
    # context tags
    'road',
    'grass',
    'footpath',
    'building',
    'water',
    'rail',
    'interior',
    'aerial',
}

ML_CONFIDENCE_THRESHOLD = 0.35


def classify_photo(file_or_bytes, meta: dict | None = None) -> dict:
    """Classify one photo. Returns {tags: [...], source: heuristic|ml}."""
    meta = meta or {}
    backend = getattr(settings, 'PHOTO_CLASSIFIER', 'heuristic')

    tags: list[str] = []
    if backend == 'ml':
        try:
            tags = _ml_tags(file_or_bytes)
            if tags:
                return {'tags': tags, 'source': 'ml'}
        except Exception:
            pass  # fall back to heuristic
    return {'tags': _heuristic_tags(meta), 'source': 'heuristic'}


# ── ML backend (pluggable) ─────────────────────────────────────────────────


def _ml_tags(file_or_bytes) -> list[str]:
    """Call the configured ML classify function, map labels -> known tags."""
    import importlib

    fn_path = getattr(settings, 'PHOTO_CLASSIFIER_ML_FN', None)
    if not fn_path:
        return []
    mod_path, _, attr = fn_path.rpartition('.')
    fn = getattr(importlib.import_module(mod_path), attr)
    data = _read_bytes(file_or_bytes)
    scores: dict[str, float] = fn(data) or {}
    # Map free-form model labels onto our tag vocabulary.
    label_map = {
        'trench': 'trench',
        'ditch': 'trench',
        'excavation': 'trench',
        'cable': 'cable',
        'wire': 'cable',
        'conduit': 'cable',
        'duct': 'cable',
        'pole': 'pole',
        'utility pole': 'pole',
        'lamppost': 'pole',
        'manhole': 'chamber',
        'vault': 'chamber',
        'handhole': 'chamber',
        'cabinet': 'pdp',
        'box': 'pdp',
        'enclosure': 'pdp',
        'house': 'premise',
        'building': 'premise',
        'road': 'road',
        'street': 'road',
        'asphalt': 'road',
        'grass': 'grass',
        'lawn': 'grass',
        'sidewalk': 'footpath',
        'bridge': 'crossing',
        'crossing': 'crossing',
        'barrier': 'obstruction',
        'fence': 'obstruction',
        'construction': 'obstruction',
    }
    tags = []
    for label, conf in sorted(scores.items(), key=lambda kv: -kv[1]):
        if conf < ML_CONFIDENCE_THRESHOLD:
            continue
        tag = label_map.get(str(label).strip().lower())
        if tag and tag not in tags:
            tags.append(tag)
    return tags[:8]


def _read_bytes(f) -> bytes:
    if isinstance(f, (bytes, bytearray)):
        return bytes(f)
    pos = f.tell() if hasattr(f, 'tell') else 0
    data = f.read()
    if hasattr(f, 'seek'):
        f.seek(pos)
    return data


# ── Heuristic backend ──────────────────────────────────────────────────────


def _heuristic_tags(meta: dict) -> list[str]:
    """Metadata-driven tagger.

    Signals used (all optional):
      layer_id / layer_name — the HLD layer being surveyed strongly implies
        what the photo shows (final_trenches -> trench, objects -> premise…)
      description / notes   — free text keywords
      asset_type            — brownfield attribute
      has_gps + near_road   — context
    """
    text = ' '.join(
        str(meta.get(k) or '')
        for k in ('description', 'notes', 'asset_type', 'layer_id', 'layer_name', 'label')
    ).lower()

    tags: list[str] = []

    keyword_map = [
        (('trench', 'graben', ' excavation', 'ditch', 'microtrench', 'micro-trench'), 'trench'),
        (('cable', 'kabel', 'duct', 'conduit', 'rohr'), 'cable'),
        (('pole', 'mast', 'lampe', 'lamp', 'utility'), 'pole'),
        (('chamber', 'manhole', 'schacht', 'handhole', 'vault', 'kf'), 'chamber'),
        (('pdp', 'kabelverteilung', 'cabinet', 'verteiler', 'gvk'), 'pdp'),
        (('house', 'gebaeude', 'gebäude', 'premise', 'address', 'hnr'), 'premise'),
        (
            ('crossing', 'kreuzung', 'ueberquer', 'überquer', 'bridge', 'bruecke', 'brücke'),
            'crossing',
        ),
        (('obstruct', 'barrier', 'block', 'hindern', 'construction', 'baustelle'), 'obstruction'),
        (('road', 'strasse', 'straße', 'asphalt', 'carriageway'), 'road'),
        (('grass', 'rasen', 'green', 'garten', 'garden'), 'grass'),
        (('footpath', 'sidewalk', 'gehweg', 'buergersteig', 'bürgersteig', 'paving'), 'footpath'),
        (('water', 'river', 'fluss', 'stream', 'canal'), 'water'),
        (('rail', 'gleis', 'bahn', 'tram', 'schiene'), 'rail'),
        (('aerial', 'luft', 'overhead', 'pole route'), 'aerial'),
    ]
    for keywords, tag in keyword_map:
        if any(k in text for k in keywords) and tag not in tags:
            tags.append(tag)

    # Layer-name inference when text gave nothing layer-specific.
    layer = str(meta.get('layer_name') or meta.get('layer_id') or '').lower()
    if layer:
        layer_map = [
            (('trench',), 'trench'),
            (('duct',), 'cable'),
            (('cable',), 'cable'),
            (('chamber',), 'chamber'),
            (('pdp',), 'pdp'),
            (('object', 'premise', 'address'), 'premise'),
            (('mfg',), 'pdp'),
            (('pole',), 'pole'),
        ]
        for keys, tag in layer_map:
            if any(k in layer for k in keys) and tag not in tags:
                tags.append(tag)
                break

    return tags[:6]


def tag_text_prefix(tags: list[str]) -> str:
    """Prefix line stored with evidence so tags survive text search."""
    return '[tags: ' + ' '.join(tags) + '] ' if tags else ''
