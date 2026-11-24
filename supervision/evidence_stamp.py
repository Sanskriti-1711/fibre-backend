"""Stamp a stored evidence photo with what the record says about it.

Crews already photograph a tape with a NoteCam panel printed on the picture:
latitude, longitude, time, section, chamber and remark burned in by the camera.
This draws the same panel for photos uploaded through the app, from the row
instead of from the camera -- so the reading, the place and the time travel with
the image into a chat, a report or a permit file.

The uploaded bytes are never rewritten: the row's sha256 is the proof of what
was submitted, so the panel goes on a sibling ``*_annotated.jpg``. The gallery
shows that copy, and the original stays one URL away.
"""

from __future__ import annotations

import hashlib
import logging
import os
from types import SimpleNamespace

from django.core.files.base import ContentFile
from django.utils import timezone

from config.media import media_url
from supervision import photo_stamp

logger = logging.getLogger(__name__)

ANNOTATED_SUFFIX = '_annotated.jpg'
TITLE = 'FIBRE FTTH | SITE EVIDENCE'


def annotated_name(image_name: str, digest: str = '') -> str:
    """The sibling object name that carries the panel.

    The panel changes when the tape is read, so the name carries a digest of
    the stamped bytes: a gallery or browser that cached the upload-time copy
    is handed a new URL for the new panel instead of the old picture.
    """
    stem, _extension = os.path.splitext(image_name)
    suffix = f'_{ANNOTATED_SUFFIX}' if not digest else f'_{digest[:8]}{ANNOTATED_SUFFIX}'
    return f'{stem}{suffix}'


def annotated_url(photo) -> str:
    """The URL of the stamped copy, or '' when the photo has none."""
    name = getattr(photo, 'annotated_image', '') or ''
    image = getattr(photo, 'image', None)
    if not name or image is None:
        return ''
    return media_url(SimpleNamespace(storage=image.storage, name=name))


def _coordinates(latitude: float, longitude: float) -> str:
    return (
        f"{abs(latitude):.6f} {'N' if latitude >= 0 else 'S'}, "
        f"{abs(longitude):.6f} {'E' if longitude >= 0 else 'W'}"
    )


def _point_from_geometry(geometry) -> dict | None:
    """One coordinate from a GeoJSON geometry: where the span is."""
    if not isinstance(geometry, dict):
        return None
    kind = geometry.get('type')
    coordinates = geometry.get('coordinates')
    try:
        if kind == 'Point':
            return {'latitude': float(coordinates[1]), 'longitude': float(coordinates[0])}
        if kind == 'LineString' and coordinates:
            middle = coordinates[len(coordinates) // 2]
            return {'latitude': float(middle[1]), 'longitude': float(middle[0])}
        if kind in ('MultiLineString', 'Polygon') and coordinates:
            middle = coordinates[0][len(coordinates[0]) // 2]
            return {'latitude': float(middle[1]), 'longitude': float(middle[0])}
    except (IndexError, TypeError, ValueError):
        return None
    return None


def _feature_point(feature) -> dict | None:
    """Where the work is: the laid geometry when there is one, else the design."""
    if feature is None:
        return None
    for geometry in (feature.as_built_geometry, feature.geometry):
        point = _point_from_geometry(geometry)
        if point:
            return point
    return None


def _uploaded_at(photo) -> str:
    uploaded_at = getattr(photo, 'uploaded_at', None)
    if uploaded_at is None:
        return ''
    return timezone.localtime(uploaded_at).strftime('%d-%m-%Y %H:%M')


def _headline(reading: dict | None) -> str:
    measurement = (reading or {}).get('measurement') or {}
    if not measurement:
        return 'TAPE NOT READ YET'
    return (
        f"{measurement['label'].upper()}  "
        f"{measurement['value']:g} {measurement['unit']}"
    )


def _footer(reading: dict | None) -> str:
    if (reading or {}).get('measurement'):
        return 'Read in-app from the blade. Confirm against the tape before saving.'
    return 'Panel fills in when the tape is read in the span form.'


def _rows(photo, data: bytes, reading: dict | None) -> list[tuple[str, str]]:
    reading = reading or {}
    measurement = reading.get('measurement') or {}
    rows: list[tuple[str, str]] = []

    readings = reading.get('readings') or []
    if readings:
        unit = reading.get('unit') or 'cm'
        rows.append(
            (
                'Tape',
                f"{readings[0]['value']:g} to {readings[-1]['value']:g} {unit}, "
                f"{len(readings)} marks",
            )
        )
    else:
        rows.append(('Tape', 'not read yet'))

    # Where the photo was taken beats where the span runs: a crew member can
    # stand at the chamber while the photo is of the trench. The phone's EXIF is
    # the only per-photo source, so the geometry is the fallback, labelled.
    exif = photo_stamp.exif_location(data)
    latitude, longitude = exif.get('latitude'), exif.get('longitude')
    label = 'GPS'
    if latitude is None:
        point = _feature_point(getattr(photo, 'feature', None))
        if point:
            latitude, longitude = point['latitude'], point['longitude']
            label = 'GPS (span)'
    if latitude is not None and longitude is not None:
        rows.append((label, _coordinates(latitude, longitude)))
    else:
        rows.append(('GPS', 'not recorded'))

    rows.append(('Captured', exif.get('captured_at') or _uploaded_at(photo) or 'not recorded'))

    feature = getattr(photo, 'feature', None)
    if feature is not None:
        rows.append(('Layer', feature.layer_name))
        rows.append(('Feature', str(feature.id)[:8]))
        project = getattr(feature, 'project', None)
        if project is not None:
            rows.append(('Project', project.name))

    uploader = getattr(photo, 'uploaded_by', None)
    if uploader is not None:
        rows.append(('Uploaded by', uploader.full_name or uploader.email))
    if getattr(photo, 'description', ''):
        rows.append(('Note', photo.description))
    if measurement:
        rows.append(('Confidence', f"{float(measurement['confidence']):.2f}"))
    return rows


def stamp_photo(photo, reading: dict | None = None) -> str:
    """Write the panel onto a stamped copy and return its stored name.

    ``reading`` is the tape endpoint's answer (``measurement``, ``readings``,
    ``unit``). Without one the panel still records where and when the photo was
    taken, and says the tape has not been read yet.
    """
    image = getattr(photo, 'image', None)
    if image is None or not image.name:
        return ''
    image.open('rb')
    try:
        data = image.read()
    finally:
        image.close()
    if not data:
        return ''

    stamped = photo_stamp.stamp_bytes(
        data,
        title=TITLE,
        headline=_headline(reading),
        rows=_rows(photo, data, reading),
        footer=_footer(reading),
    )
    storage = image.storage
    target = annotated_name(image.name, hashlib.sha256(stamped).hexdigest())
    previous = getattr(photo, 'annotated_image', '') or ''
    if target == previous:
        # Same bytes: the URL a client already holds is still the right one.
        return previous
    payload = ContentFile(stamped)
    payload.content_type = 'image/jpeg'
    saved = storage.save(target, payload)
    if previous and previous != saved:
        # One panel per photo: two copies would be two readings to choose from.
        storage.delete(previous)
    return saved


def stamp_and_record(photo, reading: dict | None = None) -> str:
    """``stamp_photo`` plus the column that names it, never raising.

    A photo that cannot be stamped is still evidence: the upload has already
    succeeded and the file is stored, so losing the row over a font or an S3
    hiccup would be the worse failure. The gallery then falls back to the
    original.
    """
    try:
        name = stamp_photo(photo, reading)
    except Exception:  # noqa: BLE001 - stamping must never lose the evidence
        logger.exception('stamping photo %s failed', getattr(photo, 'id', None))
        return ''
    if name and name != photo.annotated_image:
        photo.annotated_image = name
        photo.save(update_fields=['annotated_image'])
    return name
