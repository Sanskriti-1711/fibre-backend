"""
Survey attribute-anomaly detection (Tier-1 A16).

Flags captured attributes that contradict each other, the road they sit on, or
the neighbours they were surveyed with — the flagship being **trench surface ≠
road class**. Pure rules over the attributes a SurveyFeature already carries;
no ML, no LLM.

Where it runs
-------------
* **Review** — each change in the LLD review / Approval Queue payload carries an
  ``anomalies`` list next to its risk score, so a planner sees "this surface
  looks wrong" before approving.
* **Approved Survey Version freeze** — the freeze already refuses an incomplete
  reroute; ``freeze_blockers`` extends that gate to attributes, refusing the
  freeze on a contradiction (`severity == "error"`). Suspicions (`warn`) never
  block.

Relationship to the geometric authority
---------------------------------------
"What surface is this trench actually in" is decided geometrically by the
engine's lateral road cross-section model
(``HLD_Planning_01/HLDPlanning/design/surface_cross_section.py``). This module
is its attribute-level counterpart at review time: same surface vocabulary, and
the same confidence convention the classifiers use (1.0 decisive, 0.5 degraded
evidence, 0.0 unverifiable — a 0.0 is never emitted as a flag). A flag raised
here and a classification made there therefore mean the same thing.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

# ── Surface vocabulary (mirrors attr_enrich / surface_cross_section) ────────
ROAD = "road"
FOOTWAY = "footway"
GARDEN = "garden"
MIXED = "mixed"

_SURFACE_FAMILY: Dict[str, Tuple[str, ...]] = {
    ROAD: ("asphalt", "road", "carriageway", "tarmac", "full"),
    FOOTWAY: ("footway", "footpath", "sidewalk", "pavement"),
    GARDEN: ("garden", "grass", "lawn", "dirt", "unpaved", "seed"),
    MIXED: ("mixed",),
}
_REINSTATE_FAMILY: Dict[str, Tuple[str, ...]] = {
    ROAD: ("road", "full"),
    FOOTWAY: ("sidewalk", "pavement", "footpath"),
    GARDEN: ("seed",),
}

# SURFACE family -> REINSTATE families that may legitimately accompany it.
_ALLOWED_REINSTATE: Dict[str, Tuple[str, ...]] = {
    ROAD: (ROAD,),
    FOOTWAY: (FOOTWAY,),
    GARDEN: (GARDEN,),
    MIXED: (MIXED, ROAD, FOOTWAY, GARDEN),
}


def _reverse(table: Dict[str, Tuple[str, ...]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for family, words in table.items():
        for w in words:
            out[w] = family
    return out


_SURFACE_LOOKUP = _reverse(_SURFACE_FAMILY)
_REINSTATE_LOOKUP = _reverse(_REINSTATE_FAMILY)

# ── Road classes ────────────────────────────────────────────────────────────
# A pure pedestrian way can never carry road restoration; anything else is a
# carriageway-class carrier (or an unmapped street), where the trench normally
# sits on the kerb band and legitimately reports a footway surface.
PURE_FOOTWAY_CLASSES = (
    "footway", "path", "pedestrian", "sidewalk", "cycleway", "steps",
    "bridleway",
)

# ── Attribute aliases (the same key has worn several names across stages) ──
_SURFACE_KEYS = ("SURFACE", "surface", "sidewalk")
_REINSTATE_KEYS = ("REINSTATE", "reinstate")
_CONSTRUCTION_KEYS = ("trench_type", "CONSTRUCT", "USAGE_TYPE", "construction_method")
_ROAD_KEYS = ("fclass", "highway", "road_class", "ROAD_CLASS", "ROAD_CLASS_NAME")

# Neighbours must share a street before the majority rule has any meaning.
_GROUP_KEYS = ("permit_group", "route_section", "street", "STREET", "road_name", "name")

# Minimum surveyed siblings sharing a street for the neighbour rule to engage.
MIN_SIBLINGS = 3

SEVERITY_ERROR = "error"
SEVERITY_WARN = "warn"


# ── Value readers ───────────────────────────────────────────────────────────

def surface_family(value) -> Optional[str]:
    """ROAD / FOOTWAY / GARDEN / MIXED for a surface word, else None."""
    if value is None:
        return None
    return _SURFACE_LOOKUP.get(str(value).strip().lower())


def reinstate_family(value) -> Optional[str]:
    if value is None:
        return None
    return _REINSTATE_LOOKUP.get(str(value).strip().lower())


def _sources(sf) -> List[dict]:
    out = []
    for src in (getattr(sf, "survey_attributes", None),
                getattr(sf, "original_attributes", None)):
        if isinstance(src, dict) and src:
            out.append(src)
    return out


def _pick(sf, keys: Tuple[str, ...]) -> Optional[str]:
    """First non-empty value for ``keys`` — the surveyed edit wins."""
    for src in _sources(sf):
        for k in keys:
            raw = src.get(k)
            if raw not in (None, ""):
                return str(raw).strip()
    return None


def surface_value(sf) -> Tuple[Optional[str], Optional[str]]:
    """(family, raw) for the feature's surface.

    The legacy designer channel wrote the surface NAME into ``sidewalk``, and
    a boolean-ish flag is emphatically not a surface — reading one as a name is
    exactly the bug that stamped HDD road crossings ``Footpath``. So the
    ``sidewalk`` key only counts when its value actually names a surface.
    """
    for src in _sources(sf):
        for k in _SURFACE_KEYS:
            raw = src.get(k)
            if raw in (None, ""):
                continue
            fam = surface_family(raw)
            if fam is not None:
                return fam, str(raw).strip()
            if k != "sidewalk":
                # Explicit surface in an unknown vocabulary — report the word.
                return None, str(raw).strip()
    return None, None


def construction_value(sf) -> Optional[str]:
    raw = _pick(sf, _CONSTRUCTION_KEYS)
    return raw.strip().lower() if raw else None


def road_class_value(sf) -> Optional[str]:
    raw = _pick(sf, _ROAD_KEYS)
    return raw.strip().lower() if raw else None


def _group_key(sf) -> str:
    for k in _GROUP_KEYS:
        val = _pick(sf, (k,))
        if val:
            return val.strip().lower()
    return (getattr(sf, "layer_name", "") or getattr(sf, "layer_id", "") or "").strip().lower()


# ── Rules ───────────────────────────────────────────────────────────────────

def _flag(rule: str, severity: str, confidence: float, message: str, **extra) -> dict:
    return {
        "rule": rule,
        "severity": severity,
        "confidence": round(float(confidence), 2),
        "message": message,
        **extra,
    }


def _rule_construction(sf, surf_fam, surf_raw) -> Optional[dict]:
    """The construction method decides what surface is physically possible.

    A drill (HDD) is always under the carriageway; a garden drop is never on
    it. This is a contradiction inside one record, so it blocks a freeze.
    """
    cons = construction_value(sf)
    if not cons or surf_fam is None:
        return None
    if cons == "aerial":
        return None  # nothing excavated — no surface to contradict
    if cons == "hdd" and surf_fam not in (ROAD, MIXED):
        return _flag(
            "surface_vs_construction", SEVERITY_ERROR, 1.0,
            "HDD span reports a %s surface — a drill always crosses under the "
            "carriageway, so this must be road restoration." % surf_raw,
            expected="Asphalt", found=surf_raw,
        )
    if cons == "garden" and surf_fam not in (GARDEN, MIXED):
        return _flag(
            "surface_vs_construction", SEVERITY_ERROR, 1.0,
            "Garden drop reports a %s surface — an off-network drop leg is not "
            "on pavement or road." % surf_raw,
            expected="Garden", found=surf_raw,
        )
    return None


def _rule_reinstate(sf, surf_fam, surf_raw) -> Optional[dict]:
    """SURFACE and REINSTATE must agree about what gets restored."""
    if surf_fam is None:
        return None
    rein_raw = _pick(sf, _REINSTATE_KEYS)
    if not rein_raw:
        return None
    rein_fam = reinstate_family(rein_raw)
    if rein_fam is None:
        return None  # vocabulary we don't model — say nothing
    if rein_fam in _ALLOWED_REINSTATE.get(surf_fam, (surf_fam,)):
        return None
    return _flag(
        "surface_vs_reinstate", SEVERITY_ERROR, 1.0,
        "%s surface reinstated as %s — the two fields contradict each other."
        % (surf_raw, rein_raw),
        expected=("%s reinstatement" % surf_fam), found=rein_raw,
    )


def _rule_road_class(sf, surf_fam, surf_raw) -> Optional[dict]:
    """Flagship: trench surface ≠ road class.

    A footway-class way cannot carry road restoration — that is a hard
    contradiction with the mapped road. The other direction (a footway surface
    on a carriageway-class street) is the *normal* kerb-band case, so it is
    reported at low confidence as a check, not an error.
    """
    if surf_fam is None:
        return None
    rcls = road_class_value(sf)
    if not rcls:
        return None
    road_is_footway = rcls.split(";")[0].strip() in PURE_FOOTWAY_CLASSES

    if road_is_footway and surf_fam in (ROAD,):
        return _flag(
            "surface_vs_road_class", SEVERITY_WARN, 1.0,
            "Surface '%s' on a %s — a pedestrian way cannot carry road "
            "restoration." % (surf_raw, rcls),
            expected="Footway", found=surf_raw, road_class=rcls,
        )
    if surf_fam == GARDEN and not road_is_footway:
        return _flag(
            "surface_vs_road_class", SEVERITY_WARN, 0.5,
            "Garden surface on a %s — the trench claims to be off-road while "
            "the route runs along a mapped street." % rcls,
            expected="Asphalt or Footway", found=surf_raw, road_class=rcls,
        )
    if surf_fam == FOOTWAY and not road_is_footway:
        # Legitimate on the kerb band; only worth a look when the street is a
        # big carriageway where the band rule is less obvious.
        if rcls.split(";")[0].strip() in ("primary", "secondary", "trunk", "motorway"):
            return _flag(
                "surface_vs_road_class", SEVERITY_WARN, 0.5,
                "Footway surface on a %s — expected on the kerb band, but check "
                "the trench is not in the carriageway." % rcls,
                expected="Asphalt", found=surf_raw, road_class=rcls,
            )
    return None


def _rule_neighbours(sf, surf_fam, siblings_fams: List[str]) -> Optional[dict]:
    """A span whose surface disagrees with the surveyed majority on its street."""
    if surf_fam is None:
        return None
    known = [f for f in siblings_fams if f is not None]
    if len(known) < MIN_SIBLINGS:
        return None
    counts: Dict[str, int] = {}
    for f in known:
        counts[f] = counts.get(f, 0) + 1
    majority, n_maj = max(counts.items(), key=lambda kv: kv[1])
    if majority == surf_fam or n_maj * 3 < len(known) * 2:
        return None  # agree, or no two-thirds majority to disagree with
    return _flag(
        "surface_vs_neighbours", SEVERITY_WARN, 0.5,
        "Surface '%s' is out of step with the %d of %d surveyed spans on this "
        "street that report %s." % (surf_fam, n_maj, len(known), majority),
        expected=majority, found=surf_fam,
    )


# ── Public API ──────────────────────────────────────────────────────────────

def detect_anomalies(survey_features: Iterable) -> Dict[str, List[dict]]:
    """Bulk anomaly detection. Returns {survey_feature_id: [anomaly, ...]}.

    Mirrors ``risk_scoring.score_changes``: one pass over the features, no
    query per row. Never raises — an unflagged feature simply maps to [].
    """
    sfs = list(survey_features)
    fams: Dict[str, Optional[str]] = {}
    groups: Dict[str, List[str]] = {}
    for sf in sfs:
        fam, _raw = surface_value(sf)
        fams[str(sf.id)] = fam
        groups.setdefault(_group_key(sf), []).append(str(sf.id))

    out: Dict[str, List[dict]] = {}
    for sf in sfs:
        sid = str(sf.id)
        surf_fam, surf_raw = surface_value(sf)
        siblings = [fams[i] for i in groups.get(_group_key(sf), []) if i != sid]
        flags: List[dict] = []
        for rule in (
            _rule_construction(sf, surf_fam, surf_raw),
            _rule_reinstate(sf, surf_fam, surf_raw),
            _rule_road_class(sf, surf_fam, surf_raw),
            _rule_neighbours(sf, surf_fam, siblings),
        ):
            if rule is not None:
                flags.append(rule)
        out[sid] = flags
    return out


def anomaly_counts(anomalies: Iterable[dict]) -> Dict[str, int]:
    """{error: n, warn: n, checked: n} over a flat list of anomalies."""
    counts = {"error": 0, "warn": 0, "checked": 0}
    for a in anomalies:
        counts["checked"] += 1
        sev = a.get("severity")
        if sev in counts:
            counts[sev] += 1
    return counts


def freeze_blockers(survey_features: Iterable) -> List[Tuple[str, str]]:
    """(change_id, message) for every contradiction that must stop a freeze.

    Only ``error`` severity blocks — a suspicion is surfaced for review but
    never holds up a version.
    """
    blockers: List[Tuple[str, str]] = []
    for sid, flags in detect_anomalies(survey_features).items():
        for a in flags:
            if a.get("severity") == SEVERITY_ERROR:
                blockers.append((sid, a.get("message") or "attribute contradiction"))
    return blockers
