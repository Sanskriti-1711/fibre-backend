"""End-to-end check for as-built geometry (Supervision stage).

Drives the real URL routes through Django's test client — auth, validation,
serializers, the map payload and the report — and fails loudly if any step
breaks. Run from `fiber-backend/`:

    python tools/e2e_as_built.py

(`manage.py shell <` does not work for this script: the shell is IPython, which
evaluates the file cell by cell, so names do not survive from one cell to the
next. The bootstrap below is why it runs as a plain script.)

Requires a supervision project with imported features and one ENGINEER user;
it exits non-zero on the first failure.
"""

import copy
import json
import os
import re
import sys

import django

# Running as a plain script puts tools/ on sys.path, not the backend root, so
# `config` would not import. Put the project root in front.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

# Windows consoles default to cp1252 and these messages use a real minus sign
# (U+2212), which raised UnicodeEncodeError *after* the checks had passed — a
# green run reported as a failure. Degrade the character, never the verdict.
try:
    sys.stdout.reconfigure(errors='replace')
except (AttributeError, ValueError):  # pragma: no cover - redirected / older Python
    pass

from django.test import Client  # noqa: E402  (after django.setup())
from rest_framework_simplejwt.tokens import RefreshToken  # noqa: E402

from projects.models import Feature, Project  # noqa: E402
from supervision.report import geometry_length_m  # noqa: E402
from users.models import User  # noqa: E402

PROJECT_ID = '86d24045-ad07-412a-be13-ded59ba82f32'
LAYER = 'final_trenches'


def fail(message):
    print('  FAIL  %s' % message)
    sys.exit(1)


def ok(message):
    print('  PASS  %s' % message)


FE_PAGE = os.path.join(os.path.dirname(BASE_DIR), 'fiber-fe', 'audit-detail.html')
REPORTS_PAGE = os.path.join(os.path.dirname(BASE_DIR), 'fiber-fe', 'reports.html')

# Restated here rather than imported from supervision.report so the harness
# checks the contract independently: if someone narrows the metric-unit set in
# the report, this row list no longer adds up to its total and the run fails.
METRE_UNITS = {'m', 'meter', 'meters', 'metre', 'metres'}


def function_body(source, name):
    """The `function <name>(...) { ... }` text, brace-matched; None if absent."""
    start = source.find('function %s(' % name)
    if start < 0:
        return None
    brace = source.find('{', start)
    if brace < 0:
        return None
    depth = 0
    for index in range(brace, len(source)):
        char = source[index]
        if char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    return None


def check_audit_delta_sign():
    """The audit page must derive Δ in the backend's direction, in both cards.

    `deltaCell` (the live planned-vs-as-built table) and `signedDelta` (the
    decision-history snapshot) once subtracted in opposite orders, so the same
    field read +0.20 m in one card and -0.20 m in the other. The page is static
    HTML with no build step, so this reads both functions from source and
    proves they agree on planned minus as-built - the direction the as-built
    report uses and projects/services/deviation.py stores.
    """
    if not os.path.exists(FE_PAGE):
        fail('%s not found - run this from the workspace checkout' % FE_PAGE)
    with open(FE_PAGE, encoding='utf-8') as handle:
        page = handle.read()

    live = function_body(page, 'deltaCell')
    history = function_body(page, 'signedDelta')
    if not live or not history:
        fail('audit-detail.html no longer defines deltaCell and signedDelta')

    if 'var a = numeric(planned)' not in live or 'var b = numeric(measurementValue(' not in live:
        fail('deltaCell no longer reads planned into a and as-built into b')
    if not re.search(r'var\s+diff\s*=\s*a\s*-\s*b\s*;', live):
        fail('deltaCell no longer subtracts as-built from planned (a - b)')
    ok('live table derives Δ as planned minus as-built')

    if 'var p = Number(planned)' not in history or 'var a = Number(asBuilt)' not in history:
        fail('signedDelta no longer reads planned into p and as-built into a')
    if not re.search(r'return\s+p\s*-\s*a\s*;', history):
        fail('signedDelta no longer returns planned minus as-built (p - a)')
    ok('decision history derives Δ as planned minus as-built')

    if 'Δ is planned minus as-built' not in page:
        fail('the comparison caption no longer states the Δ direction')
    ok('the caption states the same Δ direction')


def close(left, right, tolerance):
    return left is not None and right is not None and abs(float(left) - float(right)) <= tolerance


def check_reports_source_badges(layers):
    """Every as-built total in the reports table says where it came from.

    A layer total summed from drawn geometry and one summed from typed length
    readings are different evidence: the first is where the span went, the
    second is what someone said it was. `report.py` labels each layer with
    `as_built_source` ('geometry' | 'measurements' | None); this proves the page
    can render every value the backend can emit, so no total is left unlabelled
    in the one table a reviewer uses to sign work off.
    """
    if not os.path.exists(REPORTS_PAGE):
        fail('%s not found - run this from the workspace checkout' % REPORTS_PAGE)
    with open(REPORTS_PAGE, encoding='utf-8') as handle:
        page = handle.read()

    body = function_body(page, 'asBuiltSource')
    if not body:
        fail('reports.html no longer defines asBuiltSource')
    if "source === 'geometry'" not in body:
        fail('asBuiltSource no longer handles source=geometry')
    if 'Laid geometry' not in body:
        fail('the geometry badge lost its label')
    if "source === 'measurements'" not in body:
        fail('asBuiltSource no longer handles source=measurements')
    if 'Length readings' not in body:
        fail('the measurements badge lost its label')
    ok('reports page maps geometry -> "Laid geometry" and measurements -> "Length readings"')

    rendered = {'geometry', 'measurements', None}
    seen = {}
    for layer in layers:
        source = layer['length']['as_built_source']
        seen[layer['layer_name']] = source
        if source not in rendered:
            fail(
                'layer %s reports as_built_source=%r, which the page renders as "—" — '
                'an unlabelled total is indistinguishable from no evidence'
                % (layer['layer_name'], source)
            )
    labels = ', '.join(
        '%s=%s' % (name, source or 'none') for name, source in sorted(seen.items())
    )
    ok('every layer source in this report is one the page labels (%s)' % labels)

    if 'asBuiltSource(length.as_built_source, length)' not in page:
        fail('the layer table no longer renders the source badge')
    ok('the layer table renders the badge from the served source')

    if 'delta_vs_as_built_m' not in page:
        fail('the BOQ card no longer prints the as-built delta')
    if 'section.delta_m' not in page:
        fail('the BOQ section table no longer prints its own delta')
    ok('the BOQ card prints the numbers checked below')


def check_boq_reconciliation(boq, layers):
    """The BOQ block must add up the way the reports page states it.

    The card prints "<x> m priced in the BOQ · <y> m recorded as laid · Δ". A
    reviewer compares that against what the crew laid, so each figure has to be
    derivable from the rows in the same payload: the total from the priced metre
    rows, each section delta from its own priced total, and the as-built side
    from the layer totals that the geometry check above just proved.
    """
    if not boq['available']:
        if not (boq.get('reason') or '').strip():
            fail('BOQ is unavailable but the report gives the page no reason to show')
        ok('BOQ unavailable, with a reason the card can print: %s' % boq['reason'][:80])
        return

    length = boq['length']

    metre_rows = [
        row for row in boq['rows'] if str(row.get('unit') or '').strip().lower() in METRE_UNITS
    ]
    priced = sum(float(row.get('quantity') or 0.0) for row in metre_rows)
    if not close(length['boq_m'], priced, 0.05):
        fail('boq_m %s != the %d priced metre row(s) %s' % (length['boq_m'], len(metre_rows), priced))
    ok('priced total %.2f m is the sum of %d metre row(s)' % (priced, len(metre_rows)))

    sections = boq['sections']
    section_priced = sum(float(section['boq_metres'] or 0.0) for section in sections)
    if not close(section_priced, length['boq_m'], 0.05):
        fail('section totals %s != boq_m %s' % (section_priced, length['boq_m']))
    ok('%d section(s) add up to the priced total' % len(sections))

    if sum(section['items'] for section in sections) != len(boq['rows']):
        fail(
            'section item counts %d != %d rows'
            % (sum(section['items'] for section in sections), len(boq['rows']))
        )
    ok('section item counts add up to all %d priced row(s)' % len(boq['rows']))

    layer_by_id = {layer['layer_id']: layer for layer in layers}
    for section in sections:
        name = section['section']
        if section['as_built_m'] is None:
            if section['delta_m'] is not None:
                fail('section %s has no as-built total but a delta of %s' % (name, section['delta_m']))
            continue
        if not close(section['delta_m'], section['boq_metres'] - section['as_built_m'], 0.05):
            fail(
                'section %s delta %s != priced %s - as-built %s'
                % (name, section['delta_m'], section['boq_metres'], section['as_built_m'])
            )
        matched = section.get('layers') or []
        if not matched:
            fail('section %s carries an as-built total but matches no layer' % name)
        for member in matched:
            layer = layer_by_id.get(member['layer_id'])
            if layer is None:
                fail('section %s matches %r, which is not a layer in this report' % (name, member['layer_id']))
        expected_planned = sum(layer_by_id[member['layer_id']]['length']['planned_m'] or 0.0 for member in matched)
        if not close(section['planned_m'], expected_planned, 0.05):
            fail(
                'section %s planned %s != its layer(s) %s'
                % (name, section['planned_m'], expected_planned)
            )
    measured = sum(1 for section in sections if section['as_built_m'] is not None)
    ok('%d section(s) reconcile priced - as-built against their own totals' % measured)

    layer_laid = sum(layer['length']['as_built_m'] or 0.0 for layer in layers)
    if not close(length['as_built_m'], layer_laid, 0.05):
        fail('boq as_built_m %s != the summed layer totals %s' % (length['as_built_m'], layer_laid))
    ok('as-built total %.3f m is the summed layer totals' % layer_laid)

    delta = length['boq_m'] - length['as_built_m']
    if not close(length['delta_vs_as_built_m'], delta, 0.05):
        fail('delta_vs_as_built_m %s != priced - as-built %s' % (length['delta_vs_as_built_m'], delta))
    expected_pct = round(delta / length['boq_m'] * 100, 2) if length['boq_m'] else None
    if not close(length['delta_pct'], expected_pct, 0.02):
        fail('delta_pct %s != %.2f%%' % (length['delta_pct'], expected_pct or 0))
    ok(
        'headline Δ %.2f m (%.2f%%) = %.2f priced - %.3f laid'
        % (delta, expected_pct or 0.0, length['boq_m'], length['as_built_m'])
    )

    # The geometry this run just submitted must reach the BOQ card: the section
    # pricing the layer it touches has to show that layer's as-built total.
    submitted = layer_by_id.get(LAYER)
    touching = [
        section
        for section in sections
        if any(member['layer_id'] == LAYER for member in (section.get('layers') or []))
    ]
    if submitted is not None and submitted['length']['as_built_m'] is not None and touching:
        section = touching[0]
        if not close(section['as_built_m'], submitted['length']['as_built_m'], 0.05):
            fail(
                'section %s shows %s m as-built but %s laid %s m'
                % (
                    section['section'],
                    section['as_built_m'],
                    LAYER,
                    submitted['length']['as_built_m'],
                )
            )
        ok(
            'the submitted geometry reaches the BOQ: %s shows %.3f m as-built'
            % (section['section'], submitted['length']['as_built_m'])
        )


def shift_north(geometry, metres=3.0):
    """The same shape laid `metres` north of the design.

    A pure lateral shift, so the laid length must match the planned length —
    which is what makes it a good probe: a length that changes means the shift
    was not lateral, i.e. the geometry was mangled.
    """
    delta = metres / 111_320.0

    def move(point):
        return [point[0], point[1] + delta] + list(point[2:])

    gtype = geometry.get('type')
    if gtype == 'LineString':
        return {'type': 'LineString', 'coordinates': [move(c) for c in geometry['coordinates']]}
    if gtype == 'MultiLineString':
        return {
            'type': 'MultiLineString',
            'coordinates': [[move(c) for c in line] for line in geometry['coordinates']],
        }
    if gtype == 'Point':
        return {'type': 'Point', 'coordinates': move(geometry['coordinates'])}
    return None


def main():
    project = Project.objects.get(id=PROJECT_ID)
    admin = User.objects.filter(is_superuser=True).first()
    engineer = User.objects.filter(role=User.Role.ENGINEER).first()
    if not (admin and engineer):
        fail('need an admin and an engineer user')

    admin_token = str(RefreshToken.for_user(admin).access_token)
    engineer_token = str(RefreshToken.for_user(engineer).access_token)
    client = Client()

    feature = (
        Feature.objects.filter(project=project, layer_name=LAYER)
        .exclude(geometry__isnull=True)
        .first()
    )
    if feature is None:
        fail('no %s feature with design geometry' % LAYER)
    print('  feature %s (%s)' % (feature.id, LAYER))

    planned = copy.deepcopy(feature.geometry)
    laid = shift_north(planned)
    if laid is None:
        fail('unsupported design geometry: %s' % planned.get('type'))
    print('  design geometry: %s' % planned['type'])

    print('== engineer submits as-built geometry ==')
    response = client.patch(
        '/api/features/%s/field-measurements/' % feature.id,
        data=json.dumps({'as_built_geometry': laid, 'comparison_notes': 'e2e as-built'}),
        content_type='application/json',
        HTTP_AUTHORIZATION='Bearer %s' % engineer_token,
    )
    if response.status_code != 200:
        fail('PATCH answered %s: %s' % (response.status_code, response.content[:300]))
    ok('PATCH accepted')
    if response.json().get('as_built_geometry') != laid:
        fail('response did not echo the stored geometry')
    ok('response carries the stored geometry')

    print('== validation ==')
    bad = client.patch(
        '/api/features/%s/field-measurements/' % feature.id,
        data=json.dumps({'as_built_geometry': {'type': 'LineString'}}),
        content_type='application/json',
        HTTP_AUTHORIZATION='Bearer %s' % engineer_token,
    )
    if bad.status_code != 400:
        fail('malformed geometry answered %s, expected 400' % bad.status_code)
    ok('malformed geometry is refused (400)')

    print('== design baseline is untouched ==')
    feature.refresh_from_db()
    if feature.geometry != planned:
        fail('the design geometry was modified by the as-built write')
    ok('design geometry unchanged')
    if feature.as_built_geometry != laid:
        fail('as-built geometry did not persist')
    ok('as-built geometry persisted in its own column')

    print('== feature detail ==')
    detail = client.get(
        '/api/projects/%s/features/%s/' % (project.id, feature.id),
        HTTP_AUTHORIZATION='Bearer %s' % admin_token,
    )
    if detail.status_code != 200:
        fail('feature detail answered %s' % detail.status_code)
    payload = detail.json()
    body = payload.get('feature', payload)
    if body.get('as_built_geometry') != laid:
        fail('feature detail does not serve as_built_geometry')
    ok('feature detail serves as_built_geometry')

    print('== map payload ==')
    map_response = client.get(
        '/api/projects/%s/map-data/' % project.id,
        HTTP_AUTHORIZATION='Bearer %s' % admin_token,
    )
    if map_response.status_code != 200:
        fail('map-data answered %s' % map_response.status_code)
    map_data = map_response.json()
    as_built = map_data.get('as_built_geojson') or {}
    if not as_built.get(LAYER, {}).get('features'):
        fail('as_built_geojson has no %s collection' % LAYER)
    laid_ids = {f['properties']['feature_id'] for f in as_built[LAYER]['features']}
    if str(feature.id) not in laid_ids:
        fail('the submitted feature is missing from as_built_geojson')
    ok('as_built_geojson carries %d feature(s) in %s' % (len(laid_ids), LAYER))
    design_ids = {
        f['properties']['feature_id'] for f in (map_data.get('geojson') or {}).get(LAYER, {}).get('features', [])
    }
    if str(feature.id) not in design_ids:
        fail('the design feature vanished from geojson')
    ok('design geojson still carries %d feature(s) — both collections served' % len(design_ids))

    print('== report ==')
    report = client.get(
        '/api/projects/%s/as-built-report/' % project.id,
        HTTP_AUTHORIZATION='Bearer %s' % admin_token,
    )
    if report.status_code != 200:
        fail('as-built report answered %s' % report.status_code)
    data = report.json()
    layer = next((row for row in data['layers'] if row['layer_name'] == LAYER), None)
    if layer is None:
        fail('report has no %s layer' % LAYER)
    length = layer['length']
    print('  length block: %s' % json.dumps(length))
    if length['as_built_source'] != 'geometry':
        fail('report did not use the laid geometry (source=%s)' % length['as_built_source'])
    ok('report reads the laid geometry')
    if length['laid_features'] < 1:
        fail('laid_features is %s' % length['laid_features'])
    # Feature level first: the shift is lateral, so THIS feature's length must be
    # unchanged. That is the proof the geometry round-tripped intact.
    feature_planned = geometry_length_m(planned)
    feature_laid = geometry_length_m(laid)
    if abs(feature_laid - feature_planned) > 0.5:
        fail('the 3 m lateral shift changed the length: %s -> %s' % (feature_planned, feature_laid))
    ok('feature length preserved by the shift (%.3f m)' % feature_laid)

    if abs((length['laid_geometry_m'] or 0) - round(feature_laid, 3)) > 0.5:
        fail(
            'laid_geometry_m %s != the submitted geometry %s'
            % (length['laid_geometry_m'], feature_laid)
        )
    ok('layer laid length %.3f m equals the submitted geometry' % feature_laid)

    # Layer level: only one feature of this layer was submitted, so the delta is
    # the whole planned length minus that one laid span — not a sign bug.
    remaining = (length['planned_m'] or 0) - (length['laid_geometry_m'] or 0)
    if length['delta_m'] is None or abs(length['delta_m'] - remaining) > 0.5:
        fail('delta_m %s != planned − laid %s' % (length['delta_m'], remaining))
    ok(
        'layer delta %.3f m = planned %.1f − laid %.1f; %d of %d features submitted'
        % (
            length['delta_m'],
            length['planned_m'] or 0,
            length['laid_geometry_m'] or 0,
            length['laid_features'],
            layer['feature_count'],
        )
    )
    if not data['totals'].get('as_built_features'):
        fail('totals.as_built_features is empty')
    ok('totals count %d feature(s) with as-built geometry' % data['totals']['as_built_features'])

    # The report above proves the backend's Δ direction. The audit page renders
    # its own Δ twice (live table + decision history) and once disagreed in
    # sign, so the page is checked here too rather than left to a code review.
    print('== reports page source badges ==')
    check_reports_source_badges(data['layers'])

    print('== BOQ reconciliation ==')
    check_boq_reconciliation(data['boq'], data['layers'])

    print('== audit page Δ convention ==')
    check_audit_delta_sign()

    print('\nAll as-built geometry checks passed for feature %s.' % feature.id)


main()
