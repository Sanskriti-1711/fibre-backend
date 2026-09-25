"""Berlin Bezirk templates for permit packages (P17).

Static mapping of the 12 Berlin Bezirke to the responsible
Strassenbaulasttraeger contact + fee notes. Used by the permit package
generators and the advisory checklist to inject a Bezirk-specific header
into application forms (Aufbruchgenehmigung) and to surface the correct
office/fees in the Copilot.

Data is curated from public Bezirksamt / SenMVKU sources (Dec 2024 state).
Fees vary by scope/duration/surface — every note says "confirm with the
authority" so a stale figure never becomes a commitment. Keep this plain
dict — no DB migration, no OSM join, just deterministic text.

Usage:
    get_bezirk("Tempelhof-Schöneberg") -> Bezirk dict or None
    bezirk_fee_note("Mitte") -> str
    bezirk_contact_block("Pankow") -> html snippet
"""

from __future__ import annotations

import html as _html
import re
from typing import Any

# ── 12 Bezirke — keyed by canonical name ─────────────────────────────────
# Contact addresses are the Straßen- und Grünflächenamt / Tiefbauamt seat
# that handles Sondernutzung / Aufbruchgenehmigung. Email/phone are the
# generic poststelle — the form leaves a blank for the case worker.
BERLIN_BEZIRKE: dict[str, dict[str, Any]] = {
    "Mitte": {
        "name": "Mitte",
        "canonical": "Mitte",
        "authority_code": "DE-ROAD-BEZIRK-BE",
        "office": "Bezirksamt Mitte von Berlin — Straßen- und Grünflächenamt",
        "address": "Karl-Marx-Allee 31, 10178 Berlin",
        "email": "strassen-gruenflaechenamt@ba-mitte.berlin.de",
        "phone": "+49 30 9018-10",
        "url": "https://www.berlin.de/ba-mitte/politik-und-verwaltung/aemter/strassen-und-gruenflaechenamt/",
        "form_name": "Antrag auf Sondernutzung / Aufbruchgenehmigung (§ 11 Berliner Straßengesetz)",
        "fee_note": "Verwaltungsgebühr nach Sondernutzungsgebühren-VO Berlin (ca. 50–300 € je Maßnahme zzgl. verkehrsrechtliche Anordnung ca. 30–150 €); Kaution/Wiederherstellung nach Aufwand (Richtwert 10–15 €/m²). Maßgebend ist der Gebührenbescheid des Bezirksamts.",
        "responsibility": "Gemeindestraßen, Geh-/Radwege, verkehrsberuhigte Bereiche (fclass residential/unclassified/footway/cycleway/pedestrian)",
    },
    "Friedrichshain-Kreuzberg": {
        "name": "Friedrichshain-Kreuzberg",
        "canonical": "Friedrichshain-Kreuzberg",
        "authority_code": "DE-ROAD-BEZIRK-BE",
        "office": "Bezirksamt Friedrichshain-Kreuzberg — Straßen- und Grünflächenamt",
        "address": "Yorckstraße 4-11, 10965 Berlin (Post: Frankfurter Allee 35/37, 10247 Berlin)",
        "email": "sga@ba-fk.berlin.de",
        "phone": "+49 30 90298-0",
        "url": "https://www.berlin.de/ba-friedrichshain-kreuzberg/politik-und-verwaltung/aemter/strassen-und-gruenflaechenamt/",
        "form_name": "Antrag auf Sondernutzung / Aufbruchgenehmigung (§ 11 BerlStrG)",
        "fee_note": "Verwaltungsgebühr ca. 50–300 € je Maßnahme zzgl. Anordnung ca. 30–150 €; Kaution nach Aufwand (Richtwert 10–15 €/m²). Gebührenbescheid des Bezirksamts ist maßgebend.",
        "responsibility": "Gemeindestraßen, Geh-/Radwege",
    },
    "Pankow": {
        "name": "Pankow",
        "canonical": "Pankow",
        "authority_code": "DE-ROAD-BEZIRK-BE",
        "office": "Bezirksamt Pankow — Straßen- und Grünflächenamt",
        "address": "Darßer Straße 203, 13088 Berlin",
        "email": "sga@ba-pankow.berlin.de",
        "phone": "+49 30 90295-0",
        "url": "https://www.berlin.de/ba-pankow/politik-und-verwaltung/aemter/strassen-und-gruenflaechenamt/",
        "form_name": "Antrag auf Sondernutzung / Aufbruchgenehmigung (§ 11 BerlStrG)",
        "fee_note": "Verwaltungsgebühr ca. 50–300 € je Maßnahme zzgl. Anordnung ca. 30–150 €; Kaution nach Aufwand (Richtwert 10–15 €/m²).",
        "responsibility": "Gemeindestraßen, Geh-/Radwege",
    },
    "Charlottenburg-Wilmersdorf": {
        "name": "Charlottenburg-Wilmersdorf",
        "canonical": "Charlottenburg-Wilmersdorf",
        "authority_code": "DE-ROAD-BEZIRK-BE",
        "office": "Bezirksamt Charlottenburg-Wilmersdorf — Straßen- und Grünflächenamt",
        "address": "Otto-Suhr-Allee 100, 10585 Berlin",
        "email": "strassenamt@charlottenburg-wilmersdorf.de",
        "phone": "+49 30 9029-10",
        "url": "https://www.berlin.de/ba-charlottenburg-wilmersdorf/verwaltung/aemter/strassen-und-gruenflaechenamt/",
        "form_name": "Antrag auf Sondernutzung / Aufbruchgenehmigung (§ 11 BerlStrG)",
        "fee_note": "Verwaltungsgebühr ca. 50–300 € je Maßnahme zzgl. Anordnung ca. 30–150 €; Kaution nach Aufwand (Richtwert 10–15 €/m²).",
        "responsibility": "Gemeindestraßen, Geh-/Radwege",
    },
    "Spandau": {
        "name": "Spandau",
        "canonical": "Spandau",
        "authority_code": "DE-ROAD-BEZIRK-BE",
        "office": "Bezirksamt Spandau — Straßen- und Grünflächenamt",
        "address": "Carl-Schurz-Straße 2-6, 13597 Berlin",
        "email": "strassenamt@ba-spandau.berlin.de",
        "phone": "+49 30 90279-0",
        "url": "https://www.berlin.de/ba-spandau/politik-und-verwaltung/aemter/strassen-und-gruenflaechenamt/",
        "form_name": "Antrag auf Sondernutzung / Aufbruchgenehmigung (§ 11 BerlStrG)",
        "fee_note": "Verwaltungsgebühr ca. 50–300 € je Maßnahme zzgl. Anordnung ca. 30–150 €; Kaution nach Aufwand.",
        "responsibility": "Gemeindestraßen, Geh-/Radwege",
    },
    "Steglitz-Zehlendorf": {
        "name": "Steglitz-Zehlendorf",
        "canonical": "Steglitz-Zehlendorf",
        "authority_code": "DE-ROAD-BEZIRK-BE",
        "office": "Bezirksamt Steglitz-Zehlendorf — Straßen- und Grünflächenamt",
        "address": "Kirchstraße 1/3, 14163 Berlin",
        "email": "strassenamt@ba-sz.berlin.de",
        "phone": "+49 30 90299-0",
        "url": "https://www.berlin.de/ba-steglitz-zehlendorf/politik-und-verwaltung/aemter/strassen-und-gruenflaechenamt/",
        "form_name": "Antrag auf Sondernutzung / Aufbruchgenehmigung (§ 11 BerlStrG)",
        "fee_note": "Verwaltungsgebühr ca. 50–300 € je Maßnahme zzgl. Anordnung ca. 30–150 €; Kaution nach Aufwand (Richtwert 10–15 €/m²).",
        "responsibility": "Gemeindestraßen, Geh-/Radwege",
    },
    "Tempelhof-Schöneberg": {
        "name": "Tempelhof-Schöneberg",
        "canonical": "Tempelhof-Schöneberg",
        "authority_code": "DE-ROAD-BEZIRK-BE",
        "office": "Bezirksamt Tempelhof-Schöneberg — Straßen- und Grünflächenamt",
        "address": "Tempelhofer Damm 165, 12099 Berlin (John-F.-Kennedy-Platz, 10825 Berlin)",
        "email": "strassenamt@ba-ts.berlin.de",
        "phone": "+49 30 90277-0",
        "url": "https://www.berlin.de/ba-tempelhof-schoeneberg/politik-und-verwaltung/aemter/strassen-und-gruenflaechenamt/",
        "form_name": "Antrag auf Sondernutzung / Aufbruchgenehmigung (§ 11 BerlStrG)",
        "fee_note": "Verwaltungsgebühr ca. 50–300 € je Maßnahme zzgl. Anordnung ca. 30–150 €; Kaution nach Aufwand (Richtwert 10–15 €/m²). Aktuell relevant für Projekt UI-Brownfield-Verify / Mariendorf (1202 Trenches).",
        "responsibility": "Gemeindestraßen, Geh-/Radwege (fclass residential/unclassified/footway)",
    },
    "Neukölln": {
        "name": "Neukölln",
        "canonical": "Neukölln",
        "authority_code": "DE-ROAD-BEZIRK-BE",
        "office": "Bezirksamt Neukölln — Straßen- und Grünflächenamt",
        "address": "Karl-Marx-Straße 83, 12040 Berlin",
        "email": "sga@bezirksamt-neukoelln.de",
        "phone": "+49 30 90239-0",
        "url": "https://www.berlin.de/ba-neukoelln/politik-und-verwaltung/aemter/strassen-und-gruenflaechenamt/",
        "form_name": "Antrag auf Sondernutzung / Aufbruchgenehmigung (§ 11 BerlStrG)",
        "fee_note": "Verwaltungsgebühr ca. 50–300 € je Maßnahme zzgl. Anordnung ca. 30–150 €; Kaution nach Aufwand.",
        "responsibility": "Gemeindestraßen, Geh-/Radwege",
    },
    "Treptow-Köpenick": {
        "name": "Treptow-Köpenick",
        "canonical": "Treptow-Köpenick",
        "authority_code": "DE-ROAD-BEZIRK-BE",
        "office": "Bezirksamt Treptow-Köpenick — Straßen- und Grünflächenamt",
        "address": "Alt-Köpenick 21, 12555 Berlin",
        "email": "sga@ba-tk.berlin.de",
        "phone": "+49 30 90297-0",
        "url": "https://www.berlin.de/ba-treptow-koepenick/politik-und-verwaltung/aemter/strassen-und-gruenflaechenamt/",
        "form_name": "Antrag auf Sondernutzung / Aufbruchgenehmigung (§ 11 BerlStrG)",
        "fee_note": "Verwaltungsgebühr ca. 50–300 € je Maßnahme zzgl. Anordnung ca. 30–150 €; Kaution nach Aufwand.",
        "responsibility": "Gemeindestraßen, Geh-/Radwege",
    },
    "Marzahn-Hellersdorf": {
        "name": "Marzahn-Hellersdorf",
        "canonical": "Marzahn-Hellersdorf",
        "authority_code": "DE-ROAD-BEZIRK-BE",
        "office": "Bezirksamt Marzahn-Hellersdorf — Straßen- und Grünflächenamt",
        "address": "Alice-Salomon-Platz 3, 12627 Berlin",
        "email": "sga@ba-mh.berlin.de",
        "phone": "+49 30 90293-0",
        "url": "https://www.berlin.de/ba-marzahn-hellersdorf/politik-und-verwaltung/aemter/strassen-und-gruenflaechenamt/",
        "form_name": "Antrag auf Sondernutzung / Aufbruchgenehmigung (§ 11 BerlStrG)",
        "fee_note": "Verwaltungsgebühr ca. 50–300 € je Maßnahme zzgl. Anordnung ca. 30–150 €; Kaution nach Aufwand.",
        "responsibility": "Gemeindestraßen, Geh-/Radwege",
    },
    "Lichtenberg": {
        "name": "Lichtenberg",
        "canonical": "Lichtenberg",
        "authority_code": "DE-ROAD-BEZIRK-BE",
        "office": "Bezirksamt Lichtenberg — Straßen- und Grünflächenamt",
        "address": "Möllendorffstraße 6, 10367 Berlin",
        "email": "sga@lichtenberg.berlin.de",
        "phone": "+49 30 90296-0",
        "url": "https://www.berlin.de/ba-lichtenberg/politik-und-verwaltung/aemter/strassen-und-gruenflaechenamt/",
        "form_name": "Antrag auf Sondernutzung / Aufbruchgenehmigung (§ 11 BerlStrG)",
        "fee_note": "Verwaltungsgebühr ca. 50–300 € je Maßnahme zzgl. Anordnung ca. 30–150 €; Kaution nach Aufwand.",
        "responsibility": "Gemeindestraßen, Geh-/Radwege",
    },
    "Reinickendorf": {
        "name": "Reinickendorf",
        "canonical": "Reinickendorf",
        "authority_code": "DE-ROAD-BEZIRK-BE",
        "office": "Bezirksamt Reinickendorf — Straßen- und Grünflächenamt",
        "address": "Eichborndamm 215, 13437 Berlin",
        "email": "strassenamt@reinickendorf.berlin.de",
        "phone": "+49 30 90294-0",
        "url": "https://www.berlin.de/ba-reinickendorf/politik-und-verwaltung/aemter/strassen-und-gruenflaechenamt/",
        "form_name": "Antrag auf Sondernutzung / Aufbruchgenehmigung (§ 11 BerlStrG)",
        "fee_note": "Verwaltungsgebühr ca. 50–300 € je Maßnahme zzgl. Anordnung ca. 30–150 €; Kaution nach Aufwand.",
        "responsibility": "Gemeindestraßen, Geh-/Radwege",
    },
}

# ── SenMVKU (Landesebene) — for fclass secondary/tertiary ─────────────────
SENMVKU = {
    "name": "Senatsverwaltung für Mobilität, Verkehr, Klimaschutz und Umwelt (SenMVKU)",
    "canonical": "SenMVKU",
    "authority_code": "DE-ROAD-LAND-BE",
    "office": "SenMVKU Berlin — Abteilung Tiefbau",
    "address": "Am Köllnischen Park 3, 10179 Berlin",
    "email": "poststelle@senmvku.berlin.de",
    "phone": "+49 30 9025-0",
    "url": "https://www.berlin.de/sen/uvk/",
    "form_name": "Antrag auf Sondernutzung an Landesstraßen (§ 11 BerlStrG i.V.m. TKG § 127)",
    "fee_note": "Landesstraßen (secondary/tertiary) — Gebühren nach SenMVKU-Gebührenverzeichnis; Verwaltungsgebühr ca. 100–500 € je Maßnahme, zuzüglich Verkehrsanordnung. Gebührenbescheid der SenMVKU ist maßgebend.",
    "responsibility": "Landesstraßen (fclass secondary/tertiary, inkl. Kreisstraßen in Berlin)",
}

# Normalized lookup — handles umlaut variants, "Berlin", "Bezirk", hyphens.
_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


def _normalize(name: str) -> str:
    s = (name or "").strip().lower()
    # Umlaut folding for matching
    s = s.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    # Strip common prefixes
    for prefix in ("bezirksamt", "bezirk", "berlin", "land berlin"):
        s = s.replace(prefix, " ")
    s = _NORMALIZE_RE.sub(" ", s)
    s = " ".join(s.split())
    return s.replace(" ", "-")  # canonical hyphen form


# Build reverse index for lookup
_ALIAS_MAP: dict[str, str] = {}
for canonical in BERLIN_BEZIRKE:
    norm = _normalize(canonical)
    _ALIAS_MAP[norm] = canonical
    # Also map ae-folded already, but add without hyphen variant
    _ALIAS_MAP[norm.replace("-", "")] = canonical
    _ALIAS_MAP[norm.replace("-", " ")] = canonical
# Manual aliases for common variants
_ALIAS_MAP[_normalize("Tempelhof-Schoeneberg")] = "Tempelhof-Schöneberg"
_ALIAS_MAP[_normalize("Tempelhof Schoeneberg")] = "Tempelhof-Schöneberg"
_ALIAS_MAP[_normalize("Charlottenburg Wilmersdorf")] = "Charlottenburg-Wilmersdorf"
_ALIAS_MAP[_normalize("Friedrichshain Kreuzberg")] = "Friedrichshain-Kreuzberg"
_ALIAS_MAP[_normalize("Marzahn Hellersdorf")] = "Marzahn-Hellersdorf"
_ALIAS_MAP[_normalize("Steglitz Zehlendorf")] = "Steglitz-Zehlendorf"
_ALIAS_MAP[_normalize("Treptow Koepenick")] = "Treptow-Köpenick"
_ALIAS_MAP[_normalize("Treptow-Koepenick")] = "Treptow-Köpenick"


def get_bezirk(municipality: str) -> dict[str, Any] | None:
    """Resolve a municipality string to a Bezirk dict, or None.

    Municipality comes from gis.osm_admin_boundary (e.g. "Tempelhof-Schöneberg",
    "Berlin", "Steglitz-Zehlendorf") or a manual entry. Matching is
    case-insensitive, umlaut-tolerant and ignores "Bezirk(samt)/Berlin" prefixes.
    """
    if not municipality:
        return None
    norm = _normalize(municipality)
    # Direct hit
    if norm in _ALIAS_MAP:
        return BERLIN_BEZIRKE[_ALIAS_MAP[norm]]
    # Substring scan — "Tempelhof-Schöneberg (Berlin)" contains the Bezirk
    for alias_norm, canonical in _ALIAS_MAP.items():
        if alias_norm and alias_norm in norm:
            return BERLIN_BEZIRKE[canonical]
        if norm and norm in alias_norm and len(norm) >= 4:
            return BERLIN_BEZIRKE[canonical]
    return None


def get_authority_contact(municipality: str, authority_code: str = "") -> dict[str, Any] | None:
    """Return the contact dict for a municipality + authority code.

    Prefers the Bezirk match; falls back to SenMVKU for LAND codes,
    otherwise None (unknown authority stays honest).
    """
    bezirk = get_bezirk(municipality or "")
    if bezirk is not None:
        return bezirk
    # Land Berlin (secondary/tertiary) — SenMVKU
    if authority_code == "DE-ROAD-LAND-BE":
        return SENMVKU
    return None


def bezirk_fee_note(municipality: str, authority_code: str = "") -> str:
    contact = get_authority_contact(municipality, authority_code)
    if contact:
        return str(contact.get("fee_note") or "")
    return "Gebühren nach Gebührenverzeichnis der zuständigen Behörde — vor Einreichung beim Straßenbaulastträger erfragen (Gebührenbescheid maßgebend)."


def bezirk_form_hint(municipality: str, permit_type: str = "", authority_code: str = "") -> str:
    """Short checklist tail for advisory/cover text — Bezirk-aware."""
    contact = get_authority_contact(municipality, authority_code)
    if contact is None:
        return "Berlin: confirm SenMVKU vs Bezirksamt responsibility by road class (fclass mapping)"
    return (
        f"Bezirk: {contact['canonical']} — {contact['office']} ({contact['address']}). "
        f"Form: {contact['form_name']}. Gebührenhinweis: {contact['fee_note']}"
    )


def _e(v: Any) -> str:
    return _html.escape("" if v is None else str(v))


def bezirk_header_html(municipality: str, authority_code: str = "") -> str:
    """HTML snippet for permit forms — Bezirk contact + fee + responsibility.

    Returns an empty string when no Bezirk matches (caller omits the block).
    The snippet is deterministic and safe to embed in the HTML forms.
    """
    contact = get_authority_contact(municipality, authority_code)
    if contact is None:
        return ""
    office = _e(contact["office"])
    addr = _e(contact["address"])
    phone = _e(contact["phone"])
    email = _e(contact["email"])
    url = _e(contact["url"])
    resp = _e(contact["responsibility"])
    form = _e(contact["form_name"])
    fee = _e(contact["fee_note"])
    return (
        '<div style="margin:12px 0 16px;padding:10px 12px;'
        'border:1px solid #D1D5DB;background:#F9FAFB;font-size:11px;line-height:1.5;">'
        f'<strong style="font-size:12px;">{office}</strong><br/>'
        f'{addr}<br/>'
        f'Tel. {phone} \u00b7 {email}<br/>'
        f'<a href="{url}" style="color:#2563EB;word-break:break-all;">{url}</a><br/>'
        f'<span style="color:#6B7280;">Zust\u00e4ndigkeit: {resp} \u00b7 '
        f'Formular: {form}</span><br/>'
        f'<span style="color:#6B7280;">{fee} \u2014 vor Einreichung best\u00e4tigen.</span>'
        "</div>"
    )
