"""Per-layer planned-vs-as-built report and BOQ quantity reconciliation.

INTEGRATION_PLAN.md §3 Stage 8.3 and 8.4: the as-built report is the
handover deliverable, and the reason the supervision stage exists — *did we
build the quantities we designed and priced?*

Two numbers are compared per layer:

* **length** — the design geometry in ``Feature.geometry`` (geodesic metres,
  measured on the stored lon/lat) against the length the crew recorded in
  ``field_measurements``. Geometry and reading are both appends: nothing here
  writes to either.
* **every other numeric attribute** — the planned value from ``properties``
  (the LLD handoff copies those over) against the engineer's reading, summed
  per field, with the count of features that fell outside the layer's
  tolerance.

The BOQ block reconciles those totals against the project's ``BoqSnapshot``
(the priced design quantities). Sections are matched to layers by normalised
name; a BOQ section with no layer is reported as "no plan in this project"
rather than silently dropped, because an unmatched priced quantity is exactly
the kind of gap a handover review is looking for.

Read-only by construction: no ``.save()``, no status change.
"""

from __future__ import annotations

import math
import re
from datetime import timezone as dt_timezone

from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from projects.models import Feature, LayerFieldConfig, Project
from projects.services.completion_service import CompletionService
from projects.services.deviation import as_number, feature_deviation

EARTH_RADIUS_M = 6_371_008.8

# A reading counts toward the layer's length total when its key says it is a
# length and its unit is a metre. Depth/width are metres too, but adding them
# to a length total would invent a quantity nobody laid.
LENGTH_KEY = re.compile(r'(length|laid|laid_len|len_m)', re.IGNORECASE)
METRE_UNITS = {'m', 'meter', 'meters', 'metre', 'metres'}
# A key that ends in _m is metres even when the row carries no unit — the LLD
# handoff and older imports both leave `length_m` with an empty unit string.
METRE_KEY = re.compile(r'(_m$|_metres$|_meters$)', re.IGNORECASE)

NON_NUMERIC_KEYWORDS = ('id', 'name', 'code', 'type', 'status', 'date', 'note')


def _round(value, digits=3):
    if value is None:
        return None
    return round(float(value), digits)


# ----------------------------------------------------------------- geometry


def _segment_m(a, b):
    """Great-circle distance between two [lon, lat] pairs, in metres."""
    try:
        lon1, lat1 = float(a[0]), float(a[1])
        lon2, lat2 = float(b[0]), float(b[1])
    except (TypeError, ValueError, IndexError):
        return 0.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    h = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def _polyline_m(coords):
    if not isinstance(coords, (list, tuple)):
        return 0.0
    return sum(_segment_m(coords[i - 1], coords[i]) for i in range(1, len(coords)))


def geometry_length_m(geometry):
    """Geodesic length of a stored GeoJSON geometry (0 for points/polygons
    treated as areas — a point has no length, a polygon perimeter is not a
    laid quantity)."""
    if not isinstance(geometry, dict):
        return 0.0
    gtype = geometry.get('type')
    coords = geometry.get('coordinates')
    if not coords:
        return 0.0
    if gtype == 'LineString':
        return _polyline_m(coords)
    if gtype == 'MultiLineString':
        return sum(_polyline_m(line) for line in coords)
    return 0.0


# ------------------------------------------------------------------- schemas


def schema_units(schema):
    """key -> unit from a layer schema (array or legacy object shape)."""
    units = {}
    if isinstance(schema, list):
        for entry in schema:
            if isinstance(entry, dict) and entry.get('key') and entry.get('unit'):
                units[entry['key']] = str(entry['unit'])
    elif isinstance(schema, dict):
        for key, meta in schema.items():
            if isinstance(meta, dict) and meta.get('unit'):
                units[key] = str(meta['unit'])
    return units


def _is_length(field, unit, sample_value):
    """Whether one field is part of the layer's laid length."""
    key = field or ''
    unit_norm = (unit or '').strip().lower()
    if unit_norm:
        return unit_norm in METRE_UNITS and bool(LENGTH_KEY.search(key))
    return bool(METRE_KEY.search(key))


# -------------------------------------------------------------------- layers


def _tolerances(project):
    try:
        return {
            cfg.layer_id: as_number(cfg.tolerance) or 0
            for cfg in LayerFieldConfig.objects.filter(project=project)
        }
    except Exception:
        # A deploy that has not run the tolerance migration must still serve a
        # report; zero tolerance means every difference is a deviation.
        return {}


def _layer_report(project, layer_id, layer_name, tolerance, units, weight):
    totals = {
        'planned': {},
        'as_built': {},
        'planned_features': {},
        'measured_features': {},
        'outside': {},
    }
    planned_m = 0.0
    as_built_m = 0.0
    measured_length_features = 0
    feature_count = 0
    status_counts = {key: 0 for key, _ in Feature.STATUS_CHOICES}

    rows = (
        Feature.objects.filter(project=project, layer_id=layer_id)
        .values(
            'status',
            'geometry',
            'as_built_geometry',
            'properties',
            'field_measurements',
        )
        .iterator(chunk_size=500)
    )
    laid_m = 0.0
    laid_features = 0

    for row in rows:
        feature_count += 1
        status_counts[row['status']] = status_counts.get(row['status'], 0) + 1

        planned_m += geometry_length_m(row['geometry'])

        if row['as_built_geometry']:
            laid_features += 1
            laid_m += geometry_length_m(row['as_built_geometry'])

        properties = row['properties'] if isinstance(row['properties'], dict) else {}
        measurements = (
            row['field_measurements']
            if isinstance(row['field_measurements'], dict)
            else {}
        )
        reading_m = 0.0

        for key, value in measurements.items():
            number = as_number(value.get('value') if isinstance(value, dict) else value)
            if number is None:
                continue
            unit = (
                str(value.get('unit') or '')
                if isinstance(value, dict)
                else str(units.get(key) or '')
            )
            totals['as_built'][key] = totals['as_built'].get(key, 0.0) + float(number)
            totals['measured_features'][key] = totals['measured_features'].get(key, 0) + 1
            if _is_length(key, unit, number):
                reading_m += float(number)

        if reading_m:
            measured_length_features += 1
        as_built_m += reading_m

        for key, value in properties.items():
            number = as_number(value)
            if number is None:
                continue
            totals['planned'][key] = totals['planned'].get(key, 0.0) + float(number)
            totals['planned_features'][key] = totals['planned_features'].get(key, 0) + 1

    # Tolerance is a per-feature rule, so the outside count comes from the same
    # function the review screen uses — one definition of "outside", two views.
    outside = {}
    for feature in (
        Feature.objects.filter(project=project, layer_id=layer_id)
        .exclude(field_measurements={})
        .only('properties', 'field_measurements')
        .iterator(chunk_size=200)
    ):
        for item in feature_deviation(feature, tolerance)['fields']:
            if not item['within_tolerance']:
                outside[item['key']] = outside.get(item['key'], 0) + 1

    fields = []
    for key in sorted(set(totals['planned']) | set(totals['as_built'])):
        planned_total = totals['planned'].get(key)
        as_built_total = totals['as_built'].get(key)
        delta = None
        delta_pct = None
        if planned_total is not None and as_built_total is not None:
            delta = planned_total - as_built_total
            delta_pct = (delta / planned_total * 100) if planned_total else None
        fields.append(
            {
                'key': key,
                'unit': units.get(key, ''),
                'planned_total': _round(planned_total),
                'as_built_total': _round(as_built_total),
                'delta': _round(delta),
                'delta_pct': _round(delta_pct, 2),
                'planned_features': totals['planned_features'].get(key, 0),
                'measured_features': totals['measured_features'].get(key, 0),
                'outside_features': outside.get(key, 0),
            }
        )

    # The laid geometry is the strongest evidence: an engineer drew where the
    # span went. Falling back to the length readings keeps older submissions
    # (and the field app's numbers-only flow) in the report; `source` says which
    # one the number came from, so nobody has to guess.
    if laid_features:
        as_built_source = 'geometry'
        as_built_value = laid_m
    elif measured_length_features > 0:
        as_built_source = 'measurements'
        as_built_value = as_built_m
    else:
        as_built_source = None
        as_built_value = None

    # With nothing recorded yet, `as_built - 0` is not a 100 % shortfall — it is
    # missing evidence. Reporting 0 m as the difference told every layer on a
    # freshly opened project that it was fully out of tolerance.
    length_delta = (planned_m - as_built_value) if as_built_source else None
    return {
        'layer_id': layer_id,
        'layer_name': layer_name,
        'feature_count': feature_count,
        'status_counts': status_counts,
        'tolerance': _round(tolerance),
        'weight_percentage': _round(weight, 2),
        'length': {
            'planned_m': _round(planned_m),
            'as_built_m': _round(as_built_value),
            # 'geometry' | 'measurements' | null — where the laid figure came from.
            'as_built_source': as_built_source,
            'laid_geometry_m': _round(laid_m) if laid_features else None,
            'laid_features': laid_features,
            'delta_m': _round(length_delta),
            'delta_pct': _round(
                (length_delta / planned_m * 100)
                if (planned_m and length_delta is not None)
                else None,
                2,
            ),
            'measured_features': measured_length_features,
        },
        'fields': fields,
        'outside_features': max(outside.values()) if outside else 0,
    }


# ----------------------------------------------------------------------- BOQ


# Words that describe the sheet a quantity sits on rather than what it is: a
# "CIVIL WORKS — DUCTS" section is about ducts, not about works.
STOPWORD_STEMS = {
    'work',
    'civil',
    'plant',
    'element',
    'installation',
    'summary',
    'total',
    'other',
    'misc',
}


def _stems(name):
    """Word stems of a layer or section name ("final_trenches" -> trench)."""
    stems = []
    for word in re.findall(r'[a-z0-9]+', str(name or '').lower()):
        for suffix in ('ing', 'es', 's'):
            if len(word) > len(suffix) + 3 and word.endswith(suffix):
                word = word[: -len(suffix)]
                break
        stems.append(word)
    return stems


def _match_layers(section, layers):
    """Layers a BOQ section prices, by a shared word stem.

    "CIVIL WORKS — TRENCHING" prices `final_trenches`, "LINES & CABLES" prices
    the cable layers.    Stems must be at least four characters and not a sheet-filler word, so
    "duct" matches `distribution_ducts`, `feeder_ducts` and `drop_ducts` while
    "works" matches nothing. A section can price several layers, and a section
    matching nothing stays visibly unmatched rather than borrowing a total.
    """
    section_stems = {
        stem for stem in _stems(section) if len(stem) >= 4 and stem not in STOPWORD_STEMS
    }
    if not section_stems:
        return []
    return [
        layer
        for layer in layers
        if section_stems
        & {
            stem
            for stem in _stems(layer['layer_name'])
            if len(stem) >= 4 and stem not in STOPWORD_STEMS
        }
    ]


def _boq_report(project, layers):
    """Reconcile the priced BOQ against the supervision totals."""
    empty = {
        'available': False,
        'reason': 'This project has no linked HLD project, so no BOQ snapshot exists.',
        'rows': [],
        'sections': [],
        'length': {},
    }
    source = getattr(project, 'source_ftth_project_id', None)
    if not source:
        return empty

    try:
        from ftth_hld.models import BoqSnapshot

        snapshot = BoqSnapshot.objects.filter(ftth_project_id=source).first()
    except Exception as exc:  # pragma: no cover - deploy ordering
        return {**empty, 'reason': f'BOQ snapshot unavailable: {exc}'}

    if snapshot is None:
        return {
            **empty,
            'reason': 'The linked HLD project has no BOQ snapshot yet (it is '
            'created when the HLD run completes).',
        }

    rows = [row for row in (snapshot.boq_json or []) if isinstance(row, dict)]
    boq_metres = 0.0
    for row in rows:
        if str(row.get('unit') or '').strip().lower() in METRE_UNITS:
            boq_metres += float(row.get('quantity') or 0.0)

    sections = {}
    for row in rows:
        section = row.get('section') or 'Unsectioned'
        entry = sections.setdefault(
            section,
            {'section': section, 'items': 0, 'boq_metres': 0.0, 'layer': None},
        )
        entry['items'] += 1
        if str(row.get('unit') or '').strip().lower() in METRE_UNITS:
            entry['boq_metres'] += float(row.get('quantity') or 0.0)

    for entry in sections.values():
        matched = _match_layers(entry['section'], layers)
        recorded = [
            layer['length']['as_built_m']
            for layer in matched
            if layer['length']['as_built_m'] is not None
        ]
        entry['layers'] = [
            {'layer_id': layer['layer_id'], 'layer_name': layer['layer_name']}
            for layer in matched
        ]
        entry['planned_m'] = _round(
            sum(layer['length']['planned_m'] or 0.0 for layer in matched)
        )
        entry['as_built_m'] = _round(sum(recorded)) if recorded else None
        entry['boq_metres'] = _round(entry['boq_metres'])
        entry['delta_m'] = (
            _round(entry['boq_metres'] - entry['as_built_m'])
            if entry['as_built_m'] is not None
            else None
        )

    planned_m = sum(layer['length']['planned_m'] or 0.0 for layer in layers)
    as_built_m = sum(layer['length']['as_built_m'] or 0.0 for layer in layers)
    any_measured = any((layer['length']['as_built_m'] is not None) for layer in layers)
    delta = (boq_metres - as_built_m) if any_measured else None

    return {
        'available': True,
        'reason': '',
        'created_at': snapshot.created_at.isoformat() if snapshot.created_at else None,
        'rows': [
            {
                'section': row.get('section') or 'Unsectioned',
                'item_code': row.get('item_code') or '',
                'item_name': row.get('item_name') or '',
                'unit': row.get('unit') or '',
                'quantity': _round(row.get('quantity')),
                'amount': _round(row.get('amount'), 2),
            }
            for row in rows
        ],
        'sections': sorted(sections.values(), key=lambda e: e['section']),
        'length': {
            'boq_m': _round(boq_metres),
            'planned_m': _round(planned_m),
            'as_built_m': _round(as_built_m) if any_measured else None,
            # BOQ priced − what the crew recorded. Positive means the priced
            # design contains metres nobody has measured as laid. Null until
            # something is measured, for the same reason as the layer totals.
            'delta_vs_as_built_m': _round(delta),
            'delta_pct': _round(
                (delta / boq_metres * 100)
                if (any_measured and boq_metres and delta is not None)
                else None,
                2,
            ),
        },
    }


# ----------------------------------------------------------------------- view


class ProjectAsBuiltReportAPIView(APIView):
    """GET /api/projects/<project_id>/as-built-report/

    Per layer: counts, planned geometry length vs recorded length, and every
    numeric attribute as planned total / as-built total / Δ. Plus the BOQ
    reconciliation. One response, because the UI shows them together and two
    round trips could disagree mid-refresh.
    """

    def get(self, request, project_id):
        try:
            project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            return Response({'detail': 'Project not found'}, status=404)

        completion = CompletionService.calculate_project_completion(project)
        weights = {layer['layer_id']: layer.get('weight') for layer in completion['layers']}
        tolerances = _tolerances(project)

        try:
            configs = {
                cfg.layer_id: cfg.schema for cfg in LayerFieldConfig.objects.filter(project=project)
            }
        except Exception:
            configs = {}

        layer_rows = (
            Feature.objects.filter(project=project)
            .values('layer_id', 'layer_name')
            .distinct()
            .order_by('layer_name')
        )

        layers = []
        for row in layer_rows:
            layer_id = row['layer_id']
            schema = configs.get(layer_id)
            if schema is None:
                sample = (
                    Feature.objects.filter(project=project, layer_id=layer_id)
                    .exclude(field_schema__isnull=True)
                    .only('field_schema')
                    .first()
                )
                schema = sample.field_schema if sample else None
            layers.append(
                _layer_report(
                    project,
                    layer_id,
                    row['layer_name'],
                    tolerances.get(layer_id, 0),
                    schema_units(schema),
                    weights.get(layer_id),
                )
            )

        total_features = sum(layer['feature_count'] for layer in layers)
        approved = sum(layer['status_counts'].get(Feature.STATUS_APPROVED, 0) for layer in layers)

        return Response(
            {
                'project': {
                    'id': str(project.id),
                    'name': project.name,
                    'region': project.region,
                    'status': project.status,
                    'source_lld_run_id': getattr(project, 'source_lld_run_id', None),
                },
                'generated_at': timezone.now().astimezone(dt_timezone.utc).isoformat(),
                'totals': {
                    'features': total_features,
                    'approved': approved,
                    'completion_percentage': (
                        round(approved / total_features * 100, 2) if total_features else 0.0
                    ),
                    'weighted_completion': completion.get('dynamic_completion'),
                    'planned_length_m': _round(
                        sum(layer['length']['planned_m'] or 0.0 for layer in layers)
                    ),
                    'as_built_length_m': _round(
                        sum(layer['length']['as_built_m'] or 0.0 for layer in layers)
                    ),
                    'as_built_features': sum(
                        layer['length']['laid_features'] or 0 for layer in layers
                    ),
                },
                'layers': layers,
                'boq': _boq_report(project, layers),
            }
        )
