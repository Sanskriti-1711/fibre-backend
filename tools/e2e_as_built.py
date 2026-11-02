"""End-to-end check for as-built geometry (Supervision stage).

Drives the real URL routes through Django's test client — auth, validation,
serializers, the map payload and the report — and fails loudly if any step
breaks. Run from `fiber-backend/`:

    python manage.py shell < tools/e2e_as_built.py

or import it from a shell. Requires a supervision project with imported
features and one ENGINEER user; it exits non-zero on the first failure.
"""

import copy
import json
import sys

from django.test import Client
from rest_framework_simplejwt.tokens import RefreshToken

from projects.models import Feature, Project
from supervision.report import geometry_length_m
from users.models import User

PROJECT_ID = '86d24045-ad07-412a-be13-ded59ba82f32'
LAYER = 'final_trenches'


def fail(message):
    print('  FAIL  %s' % message)
    sys.exit(1)


def ok(message):
    print('  PASS  %s' % message)


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

    print('\nAll as-built geometry checks passed for feature %s.' % feature.id)


main()
