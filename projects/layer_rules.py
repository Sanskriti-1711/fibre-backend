"""Which layers a field-survey project serves.

The field survey answers questions about the trench it is standing in and the
premises it connects — ``Final_Trenches`` and ``objects``. Everything the
designer writes beside those two is an input to the cabling stages, not
something a surveyor can record against, and a Survey copy must not carry it.

Two kinds of row are excluded today:

* ``Pseudo_HH`` — the footway end of a garden leg, a point the trench designer
  derives so the duct graph has something to attach to. It has no address, no
  household and nothing a surveyor can record against it.
* ``Feeder_Trench`` / ``Distribution_Trench`` / ``Garden_Trench`` — the
  designer's per-tier MIRRORS of the same trench geometry. ``Final_Trenches``
  is the published design layer (the one ducts and cables are planned on); the
  three tiers are cabling inputs that repeat the same paths in segments. On
  project ``24cd3573…`` they are 265 + 224 + 58 rows against ``final_trenches``'
  387 — 547 of the copy's 934 trench features are the same trenches drawn a
  second, third and fourth time.

A Survey copy built before this rule carries them — project ``24cd3573…`` holds
23 layers including ``Pseudo_HH`` (224) beside ``objects`` (285) — and its
stored rows are re-served on every fetch. The rule therefore has to hold at
three points, and this module is the single source for all three:

* the package (``ftth_hld.pipeline``): the artefact is never bundled;
* the assignment import (``ftth_hld.assign``): it never becomes a Feature row;
* the project layer API (``projects.api.layers``): it is never served — which
  is what an already-installed app obeys without being rebuilt.

The lookup normalises the id the way the app's ``normalizeLayerId``
(``fibre-mobile/lib/utils/layer-ids.ts``) does, so the package's ``Pseudo_HH``,
a store prefix (``imp-pseudo_hh``) and a bare ``pseudo_hh`` are one answer.
"""

from __future__ import annotations

import re

# Layers the field survey never serves, whatever a package or a stored copy
# holds. Only design artefacts the field side does NOT use are listed: a layer
# the survey works in must never be added here by accident. `final_trenches`
# (the single published trench layer) and `trench_nodes` stay.
FIELD_SURVEY_EXCLUDED_LAYERS = (
    'pseudo_hh',
    # per-tier mirrors of the same trench geometry -- see the module docstring
    'feeder_trench',
    'distribution_trench',
    'garden_trench',
    # Trench joints, not trenches: every node is realised by a chamber, which
    # is the published layer the crew records against. The engine never
    # publishes it and the results map never shows it.
    'trench_nodes',
)

# Layers the field survey always receives. ``objects`` is the premises layer
# the whole exercise is keyed on, so a run whose published layer list omitted
# it must not hand the crew a package without premises.
FIELD_SURVEY_ALWAYS_PACKAGED_LAYERS = ('objects',)

# Mirrors the app's normaliser: store/map prefixes, CamelCase, separators.
_PREFIX_CHAIN = re.compile(r'^(?:(?:imp|survey|temp-preview|ml|src|lyr|out|drag)-)+', re.I)
_CAMEL_BOUNDARY = re.compile(r'([a-z0-9])([A-Z])')
_SEPARATORS = re.compile(r'[\s./\\-]+')
_DUPLICATE_UNDERSCORES = re.compile(r'_{2,}')


def normalize_layer_id(layer_id) -> str:
    """The app's ``normalizeLayerId``, so both sides answer the same way.

    Deliberately lossy only in ways that cannot merge two real layers — case,
    separators and the store/map prefixes. ``final_trenches`` and
    ``Final_Trenches`` are the same layer; ``trench_nodes`` and
    ``trench_sections`` are not.
    """
    text = str(layer_id or '').strip()
    text = _PREFIX_CHAIN.sub('', text)
    text = _CAMEL_BOUNDARY.sub(r'\1_\2', text)
    text = _SEPARATORS.sub('_', text)
    text = text.lower()
    text = _DUPLICATE_UNDERSCORES.sub('_', text)
    return text.strip('_')


def is_field_survey_layer(layer_id) -> bool:
    """Does the field survey serve a layer with this id?"""
    return normalize_layer_id(layer_id) not in FIELD_SURVEY_EXCLUDED_LAYERS


def field_survey_exclusion_q(field: str = 'layer_id'):
    """``is_field_survey_layer`` as a query, for aggregates that cannot filter
    in Python.

    Completion counts every Feature row of a project, so a layer the app never
    serves would cap a copy's completion by rows nobody can record against
    (project `24cd3573…`: 224 `Pseudo_HH` rows and 547 per-tier trench rows of
    3,578). The same spellings are accepted here: case-insensitive, separators
    tolerated, the app's store prefix ignored.
    """
    from django.db.models import Q

    body = '|'.join(
        re.escape(name).replace('_', '[ _-]') for name in sorted(FIELD_SURVEY_EXCLUDED_LAYERS)
    )
    return Q(**{f'{field}__iregex': f'^(?:imp-)?(?:{body})$'})
