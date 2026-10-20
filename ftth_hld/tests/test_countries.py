"""The country dropdown must not depend on the engine being awake.

Why this exists: `GET /api/ftth/hld/countries/` proxied the engine for a list
that never changes.  The gateway allows 60s for it and the page retries once
after 5s, so a sleeping engine instance produced an empty dropdown followed by
"Could not load the country list. Reload the page, or pick the city and area
name instead of a bare postcode." — a message about a form that was working
fine, caused by a cold start 40 seconds away.

The list is ISO 3166-1 data: it is served from `ftth_hld/data/countries.json`
now, so these tests pin both the content and the fact that no HTTP call is
made at all.
"""

from __future__ import annotations

from unittest import mock

from django.test import SimpleTestCase

from ftth_hld import countries, pipeline

# How many entries the engine's `countries.py` publishes: ISO 3166-1 alpha-2
# short names plus XK (Kosovo), the code OSM/Nominatim use outside ISO.
EXPECTED_COUNTRIES = 250


class CountryListTests(SimpleTestCase):
    def test_the_dropdown_gets_the_whole_list(self):
        options = pipeline.list_countries()

        self.assertEqual(len(options), EXPECTED_COUNTRIES)
        self.assertEqual(options[0], {'code': 'AF', 'name': 'Afghanistan'})

    def test_the_codes_the_area_form_depends_on_are_there(self):
        by_code = {entry['code']: entry['name'] for entry in countries.country_options()}

        self.assertEqual(by_code['GB'], 'United Kingdom')
        self.assertEqual(by_code['US'], 'United States')
        self.assertEqual(by_code['DE'], 'Germany')
        self.assertEqual(by_code['FR'], 'France')
        # Not in ISO 3166-1, but it is what OSM uses and the engine accepts it.
        self.assertEqual(by_code['XK'], 'Kosovo')

    def test_codes_are_unique_and_names_sorted(self):
        options = countries.country_options()
        codes = [entry['code'] for entry in options]
        names = [entry['name'] for entry in options]

        self.assertEqual(len(codes), len(set(codes)), 'duplicate ISO code')
        self.assertEqual(names, sorted(names), 'options must be ordered by name')
        self.assertTrue(all(len(code) == 2 for code in codes))

    def test_no_engine_call_is_made_to_fill_the_dropdown(self):
        """An engine outage must not be able to empty the dropdown."""
        with mock.patch.object(pipeline.requests, 'get') as get:
            options = pipeline.list_countries()

        self.assertEqual(len(options), EXPECTED_COUNTRIES)
        get.assert_not_called()

    def test_the_country_view_answers_without_an_engine(self):
        with mock.patch.object(pipeline.requests, 'get') as get:
            from ftth_hld.api import CountriesView

            response = CountriesView().get(request=mock.Mock())

        get.assert_not_called()
        self.assertEqual(response.status_code, 200)

    def test_a_missing_data_file_is_reported_rather_than_raised(self):
        """A broken image must say so in the log, not 500 the area form."""
        countries._entries.cache_clear()
        with mock.patch.object(countries, '_DATA_FILE') as missing:
            missing.open.side_effect = OSError('not in the image')
            self.assertEqual(countries.country_options(), [])
            with self.assertLogs('ftth_hld.pipeline', level='ERROR'):
                self.assertEqual(pipeline.list_countries(), [])
        countries._entries.cache_clear()
