"""Auto-populated permit application forms.

One HTML form per **street-level permit group** (rule × ``permit_group``),
pre-filled from the project, the authority, the rule and the recorded
evidence — the deterministic permit-determination data (never LLM-generated).
Covered route sections are listed inside the form, so a street with 40 trench
segments yields one form instead of 40. Forms are printable and carry a
traceability footer (rule id + version + analysis timestamp).
"""

from __future__ import annotations

import html as _html
from datetime import UTC, datetime
from typing import Any

from . import data

# Bezirk templates are optional — the forms degrade gracefully when the
# bezirke module is unavailable (tests / older checkouts).
try:
    from ..bezirke import SENMVKU as _SENMVKU  # type: ignore
    from ..bezirke import bezirk_fee_note as _bezirk_fee_note  # type: ignore
    from ..bezirke import bezirk_header_html as _bezirk_header_html  # type: ignore
    from ..bezirke import get_bezirk as _get_bezirk  # type: ignore

    _HAS_BEZIRKE = True
except ImportError:  # pragma: no cover
    _bezirk_header_html = lambda *_a, **_kw: ''  # type: ignore
    _get_bezirk = lambda *_a, **_kw: None  # type: ignore
    _bezirk_fee_note = lambda *_a, **_kw: ''  # type: ignore
    _SENMVKU = {}  # type: ignore
    _HAS_BEZIRKE = False


def _e(v: Any) -> str:
    return _html.escape('' if v is None else str(v))


def _evidence_rows(pm) -> list[tuple[str, str]]:
    ev = pm.evidence or {}
    rows = []
    for key, val in ev.items():
        if isinstance(val, dict):
            present = val.get('present')
            value = val.get('value')
            if present and value is not None:
                rows.append((key.replace('_', ' ').title(), _e(value)))
            elif present:
                rows.append((key.replace('_', ' ').title(), 'Recorded'))
            else:
                rows.append((key.replace('_', ' ').title(), '—'))
        else:
            rows.append((key.replace('_', ' ').title(), _e(val)))
    return rows


def _form_page(pm, project_name: str, members: list | None = None) -> str:
    authority = pm.authority
    rule = pm.rule
    now = datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')
    status = pm.get_status_display() if hasattr(pm, 'get_status_display') else str(pm.status)
    members = members or []

    def field(label: str, value: str) -> str:
        return (
            f'<tr><th style="text-align:left;width:200px;padding:6px 8px;'
            f'border:1px solid #E5E7EB;background:#F9FAFB;font-size:12px;">'
            f'{_e(label)}</th>'
            f'<td style="padding:6px 8px;border:1px solid #E5E7EB;font-size:12px;">'
            f'{value}</td></tr>'
        )

    group_label = pm.permit_group or 'Unnamed section'
    # Bezirk contact block — shown only when the municipality matches a Berlin
    # Bezirk (e.g. Tempelhof-Schöneberg). Keeps the form honest and saves the
    # planner from looking up the office.
    try:
        _bezirk_block = (
            _bezirk_header_html(pm.municipality or '', authority.code if authority else '')
            if _HAS_BEZIRKE
            else ''
        )
    except Exception:
        _bezirk_block = ''
    rows = ''.join(
        [
            field('Project', _e(project_name)),
            field('Project ID', _e(pm.project_id)),
            field('Permit Type', _e(pm.permit_type)),
            field('Route / Street', _e(group_label)),
            field('Covered Sections', f'{len(members)}'),
            field('Layer', _e(pm.layer)),
            field('Municipality', _e(pm.municipality or 'To be resolved')),
            field('Authority', _e(authority.name if authority else 'Unassigned')),
            field('Authority Code', _e(authority.code if authority else '—')),
            field('Required Level', _e(rule.required_level if rule else '')),
            field('Status', _e(status)),
            field('Readiness', f'{pm.readiness_pct}%'),
            field('Conditions', _e(pm.conditions or '—')),
            field('Comments', _e(pm.comments or '—')),
        ]
    )

    ev_rows = (
        ''.join(
            f'<tr><td style="padding:4px 8px;border:1px solid #E5E7EB;font-size:12px;">'
            f'{k}</td><td style="padding:4px 8px;border:1px solid #E5E7EB;font-size:12px;">'
            f'{v}</td></tr>'
            for k, v in _evidence_rows(pm)
        )
        or '<tr><td colspan="2" style="padding:6px 8px;font-size:12px;color:#6B7280;">No evidence recorded yet.</td></tr>'
    )

    member_rows = ''.join(
        f'<tr><td style="padding:4px 8px;border:1px solid #E5E7EB;font-size:12px;">'
        f'{_e(m.route_section)}</td>'
        f'<td style="padding:4px 8px;border:1px solid #E5E7EB;font-size:12px;">'
        f'{_e(m.get_status_display())}</td>'
        f'<td style="padding:4px 8px;border:1px solid #E5E7EB;font-size:12px;">'
        f'{m.readiness_pct}%</td></tr>'
        for m in members
    )

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<title>Permit Application — {_e(pm.permit_type)}</title></head>
<body style="font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#111827;">
  <h2 style="margin:0 0 4px;">Permit Application Form</h2>
  <p style="margin:0 0 16px;color:#6B7280;font-size:12px;">Auto-generated by the FTTH permit engine — deterministic data only.</p>
  {_bezirk_block}
  <table style="border-collapse:collapse;width:100%;">{rows}</table>
  <h3 style="margin:20px 0 8px;font-size:14px;">Covered Route Sections ({len(members)})</h3>
  <table style="border-collapse:collapse;width:100%;">
    <tr style="background:#F9FAFB;">
      <th style="text-align:left;padding:6px 8px;border:1px solid #E5E7EB;font-size:11px;">Section</th>
      <th style="text-align:left;padding:6px 8px;border:1px solid #E5E7EB;font-size:11px;">Status</th>
      <th style="text-align:left;padding:6px 8px;border:1px solid #E5E7EB;font-size:11px;">Readiness</th>
    </tr>
    {member_rows}
  </table>
  <h3 style="margin:20px 0 8px;font-size:14px;">Evidence</h3>
  <table style="border-collapse:collapse;width:100%;">{ev_rows}</table>
  <p style="margin-top:24px;font-size:10px;color:#9CA3AF;">
    Traceability: rule {_e(rule.rule_id if rule else '—')} v{_e(rule.version if rule else '—')} · generated {now}
  </p>
</body></html>
"""


def application_forms(project_id: str, project_name: str) -> list[dict[str, Any]]:
    """One HTML application form per street-level permit group.

    Groups the matrix by (rule, ``permit_group``) so a street with many
    trench segments yields a single form listing its covered sections.
    UTILITY_REUSE rows are informational and never get an application form.
    """
    rows = [
        pm
        for pm in data.permit_rows(project_id)
        if pm.rule and pm.rule.rule_id != 'UTILITY_REUSE_001'
    ]
    groups: dict[tuple, list] = {}
    for pm in rows:
        groups.setdefault((pm.rule_id, pm.permit_group or ''), []).append(pm)

    forms = []
    for (rule_id, group), members in groups.items():
        head = members[0]
        slug = (head.permit_type or 'permit').lower().replace(' ', '_')
        gslug = (group or 'unnamed').lower().replace(' ', '_')[:48] or 'unnamed'
        forms.append(
            {
                'name': f'form_{slug}_{gslug}',
                'kind': 'FORM',
                'filename': f'forms/{slug}_{gslug}.html',
                'content': _form_page(head, project_name, members),
                'description': (
                    f"Application form — {head.permit_type} ({group or 'unnamed'}, "
                    f"{len(members)} sections)"
                ),
            }
        )
    return forms


# ── German-Standard Forms ────────────────────────────────────────────────


def hld_permit_overview(project_id: str, project_name: str) -> dict[str, Any]:
    """HLD-stage permit overview — one downloadable summary per project.

    Lists all identified permits with their authorities, readiness, and
    the evidence still needed. This is the "rough output" handover from
    the HLD planner to the permit team before the survey starts.
    Uses DIN 5008 / German business-letter layout conventions.
    """
    rows = data.permit_rows(project_id)
    now = datetime.now(UTC).strftime('%d.%m.%Y, %H:%M')

    # Summarize by permit type
    by_type: dict[str, dict[str, Any]] = {}
    for pm in rows:
        ptype = pm.permit_type or 'Unknown'
        b = by_type.setdefault(
            ptype,
            {
                'count': 0,
                'ready': 0,
                'streets': set(),
                'authorities': set(),
                'evidence_missing': [],
            },
        )
        b['count'] += 1
        if pm.status == 'READY' or pm.status == 'APPROVED':
            b['ready'] += 1
        if pm.permit_group:
            b['streets'].add(pm.permit_group)
        if pm.authority:
            b['authorities'].add(pm.authority.name)
        rule = pm.rule or {}
        required = list(getattr(rule, 'evidence_required', None) or [])
        for key in required:
            if not (pm.evidence or {}).get(key, {}).get('present'):
                if key not in b['evidence_missing']:
                    b['evidence_missing'].append(key)

    type_rows = ''
    for ptype, b in sorted(by_type.items()):
        type_rows += (
            f'<tr><td style="padding:6px 8px;border:1px solid #CCC;font-size:12px;">'
            f"{_e(ptype)}</td>"
            f'<td style="padding:6px 8px;border:1px solid #CCC;font-size:12px;text-align:center;">'
            f'{b["count"]}</td>'
            f'<td style="padding:6px 8px;border:1px solid #CCC;font-size:12px;text-align:center;">'
            f'{b["ready"]}</td>'
            f'<td style="padding:6px 8px;border:1px solid #CCC;font-size:12px;">'
            f'{_e(", ".join(sorted(b["streets"])[:5]))}'
            f'{"..." if len(b["streets"]) > 5 else ""}</td>'
            f'<td style="padding:6px 8px;border:1px solid #CCC;font-size:12px;">'
            f'{_e(", ".join(sorted(b["authorities"])[:3]))}'
            f'{"..." if len(b["authorities"]) > 3 else ""}</td>'
            f'{_e(", ".join(b["evidence_missing"][:5]) or chr(8212))}</td>'
            f"</tr>"
        )

    content = f"""<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8"/>
<title>HLD Permitu00fcbersicht \u2013 {_e(project_name)}</title>
<style>
  body {{ font-family:'Segoe UI','Helvetica Neue',Arial,sans-serif; margin:32px 40px; color:#111; }}
  h1 {{ font-size:20px; margin:0 0 4px; }}
  h2 {{ font-size:14px; margin:28px 0 8px; border-bottom:1px solid #CCC; padding-bottom:4px; }}
  table {{ border-collapse:collapse; width:100%; }}
  th {{ background:#F2F2F2; font-size:11px; font-weight:600; }}
  .footer {{ margin-top:32px; font-size:10px; color:#888; border-top:1px solid #E0E0E0; padding-top:8px; }}
</style></head><body>
<h1>Permitu00fcbersicht (HLD-Stufe)</h1>
<p style="font-size:12px;color:#666;margin:0;">
  Projekt: {_e(project_name)} &middot; Stand: {now} &middot;
  {len(rows)} Genehmigungszeilen in {len(by_type)} Kategorien
</p>

<h2>1. Zusammenfassung</h2>
<table>
  <tr>
    <th style="text-align:left;padding:6px 8px;border:1px solid #CCC;">Genehmigungstyp</th>
    <th style="text-align:center;padding:6px 8px;border:1px solid #CCC;">Anzahl</th>
    <th style="text-align:center;padding:6px 8px;border:1px solid #CCC;">Bereit</th>
    <th style="text-align:left;padding:6px 8px;border:1px solid #CCC;">Strasse / Bereich</th>
    <th style="text-align:left;padding:6px 8px;border:1px solid #CCC;">Zust. Behu00f6rde</th>
    <th style="text-align:left;padding:6px 8px;border:1px solid #CCC;">Fehlende Nachweise</th>
  </tr>
  {type_rows}
</table>

<h2>2. Nu00e4chste Schritte</h2>
<ol style="font-size:12px;line-height:1.8;">
  <li><strong>Survey:</strong> Ingenieur sammelt vor Ort: Fotos, Grabenmasse,
      Oberflu00e4chenmaterial, Verkehrsaufkommen, Querungen.</li>
  <li><strong>LLD:</strong> Ausfu00fchrungsplanung erzeugt Lageplu00e4ne,
      Querschnitte, Verkehrsfu00fchrungsplu00e4ne und den formalen
      Genehmigungsantrag pro Strasse.</li>
  <li><strong>Einreichung:</strong> Antru00e4ge gebu00fcndelt pro Beh\u00f6rde
      und Strasse einreichen (siehe Submission-Tracker).</li>
</ol>

<p class="footer">
  Automatisch generiert vom FTTH Permit Engine \u2022
  Keine rechtsverbindliche Auskunft \u2022
  Nur fu00fcr interne Planungszwecke
</p>
</body></html>"""

    return {
        'name': f'hld_permit_overview_{project_name[:32]}',
        'kind': 'FORM',
        'filename': 'forms/hld_permit_overview.html',
        'content': content,
        'description': (
            f'HLD-Stufe Permitu00fcbersicht \u2013 {len(rows)} Genehmigungszeilen, '
            f'{len(by_type)} Kategorien'
        ),
    }


def german_street_opening_form(project_id: str, project_name: str) -> list[dict[str, Any]]:
    """German-standard street-opening permit (Aufbruchgenehmigung).

    One form per street-level ROAD permit group at LLD stage, patterned
    after the real municipal application forms (Antrag auf
    Sondernutzung / Aufbruchgenehmigung nach u00a7 18 StrWG NRW / TKG u00a7 127).
    Includes pre-populated: applicant, project, trench dimensions,
    surface/reinstatement, traffic impact, and a supporting-documents
    checklist.
    """
    rows = [
        pm for pm in data.permit_rows(project_id) if pm.permit_type == 'Road Opening' and pm.rule
    ]
    trench = data.trench_stats(project_id)
    groups: dict[tuple, list] = {}
    for pm in rows:
        groups.setdefault((pm.permit_group or 'Unnamed', pm.municipality or ''), []).append(pm)

    forms = []
    now = datetime.now(UTC).strftime('%d.%m.%Y')
    for (street, municipality), members in groups.items():
        head = members[0]
        total_m = sum(
            float((pm.evidence or {}).get('length_m', {}).get('value', 0) or 0) for pm in members
        ) or round(trench.get('total_length_m', 0) / max(len(groups), 1), 1)

        # Bezirk contact block for the Aufbruch form
        try:
            _aufbruch_bezirk = (
                _bezirk_header_html(
                    municipality or '', head.authority.code if head.authority else ''
                )
                if _HAS_BEZIRKE
                else ''
            )
        except Exception:
            _aufbruch_bezirk = ''
        section_rows = ''
        for pm in members[:30]:
            section_rows += (
                f'<tr><td style="padding:3px 6px;border:1px solid #CCC;font-size:11px;">'
                f'{_e(pm.route_section)}</td>'
                f'<td style="padding:3px 6px;border:1px solid #CCC;font-size:11px;">'
                f'{_e(pm.status)}</td></tr>'
            )

        content = f"""<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8"/>
<title>Antrag auf Aufbruchgenehmigung \u2013 {_e(street)}</title>
<style>
  * {{ box-sizing:border-box; }}
  body {{ font-family:'Segoe UI','Helvetica Neue',Arial,sans-serif; margin:24px 32px; color:#111; font-size:12px; }}
  h1 {{ font-size:18px; margin:0 0 2px; }}
  h2 {{ font-size:13px; margin:22px 0 6px; border-bottom:1px solid #888; padding-bottom:3px; }}
  .grid {{ display:grid; grid-template-columns:200px 1fr; gap:0; border:1px solid #888; }}
  .grid .lbl {{ background:#EEE; padding:5px 8px; border-bottom:1px solid #CCC; font-weight:600; }}
  .grid .val {{ padding:5px 8px; border-bottom:1px solid #CCC; }}
  .checklist {{ list-style:none; padding:0; }}
  .checklist li {{ padding:4px 0; }}
  .footer {{ margin-top:28px; font-size:9px; color:#888; border-top:1px solid #CCC; padding-top:6px; }}
  @media print {{ body {{ margin:12px 16px; }} }}
</style></head><body>

<h1>Antrag auf Aufbruchgenehmigung</h1>
<p style="font-size:11px;color:#555;margin:0;">
  gem. u00a7 18 StrWG / TKG u00a7 127 &middot; generiert am {now}
</p>
{_aufbruch_bezirk}

<h2>1. Antragsteller</h2>
<div class="grid">
  <div class="lbl">Firma / Betreiber</div><div class="val">[Bitte eintragen]</div>
  <div class="lbl">Ansprechpartner</div><div class="val">[Name, Tel., E-Mail]</div>
  <div class="lbl">Projekt</div><div class="val">{_e(project_name)}</div>
  <div class="lbl">Projekt-ID</div><div class="val">{_e(project_id)}</div>
</div>

<h2>2. Massnahme</h2>
<div class="grid">
  <div class="lbl">Strasse</div><div class="val">{_e(street)}</div>
  <div class="lbl">Gemeinde / Bezirk</div><div class="val">{_e(municipality or "[aus OSM / Projekt]")}</div>
  <div class="lbl">Zust. Beh\u00f6rde</div><div class="val">{_e(head.authority.name if head.authority else "[siehe Matrix]")}</div>
  <div class="lbl">Massnahme</div><div class="val">Verlegung von Glasfaserkabeln (FTTH-Ausbau)</div>
  <div class="lbl">Bauweise</div>
  <div class="val">
    {_e(head.evidence.get("construction_method", {}).get("value", "")) if head.evidence else ""}
    &nbsp;(Tiefbau / Micro-Trenching / HDD nach Plan)
  </div>
  <div class="lbl">Gesamtlu00e4nge</div><div class="val">{total_m} m</div>
  <div class="lbl">Geplante Dauer</div><div class="val">[Tage / von\u2013bis eintragen]</div>
  <div class="lbl">Anzahl Abschnitte</div><div class="val">{len(members)}</div>
</div>

<h2>3. Technische Angaben</h2>
<div class="grid">
  <div class="lbl">Grabenbreite</div><div class="val">{trench.get("max_width_mm", "–")} mm</div>
  <div class="lbl">Grabentiefe</div><div class="val">{trench.get("max_depth_mm", "–")} mm</div>
  <div class="lbl">Oberflu00e4che</div><div class="val">{_e(", ".join(trench.get("by_surface", {}).keys()))}</div>
  <div class="lbl">Wiederherstellung</div><div class="val">Vollst. Wiederherstellung nach ZTV A-StB / RStO</div>
  <div class="lbl">Kabeltyp</div><div class="val">LWL-Einblasrohr / Mikrorohr / HDPE-Rohrverbund</div>
  <div class="lbl">Leerrohre</div><div class="val">Details siehe BOQ &middot; Trassenplan (Anlage 1)</div>
</div>

<h2>4. Verkehrsrechtliche Anordnung</h2>
<p style="font-size:11px;">
  [ ] Verkehrsfu00fchrungsplan (Anlage 2) liegt bei<br/>
  [ ] Halteverbotszone beantragt<br/>
  [ ] Fusg\u00e4ngerumleitung eingerichtet<br/>
  [ ] Beschilderungsplan (Anlage 3)
</p>

<h2>5. Anlagen (Checkliste)</h2>
<ul class="checklist">
  <li>[ ] Anlage 1: Lageplan / Trassenplan (Masstab 1:500)</li>
  <li>[ ] Anlage 2: Verkehrsfu00fchrungsplan (TMP)</li>
  <li>[ ] Anlage 3: Beschilderungsplan</li>
  <li>[ ] Anlage 4: Querschnitt / Regelzeichnung</li>
  <li>[ ] Anlage 5: Leitungsbestandsplan (Spartenauskunft)</li>
  <li>[ ] Anlage 6: BOQ / Kostenschu00e4tzung</li>
  <li>[ ] Anlage 7: Fotodokumentation (vorher / wu00e4hrend / nachher)</li>
  <li>[ ] Anlage 8: Zustimmung des Grundeigu00fcntmers (falls privat)</li>
  <li>[ ] Anlage 9: Stellungnahme TKG / Wegerecht</li>
</ul>

<h2>6. Betroffene Abschnitte ({len(members)})</h2>
<table style="border-collapse:collapse;width:100%;">
  <tr style="background:#EEE;">
    <th style="text-align:left;padding:4px 6px;border:1px solid #CCC;">Abschnitt</th>
    <th style="text-align:left;padding:4px 6px;border:1px solid #CCC;">Status</th>
  </tr>
  {section_rows}
</table>

<div style="margin-top:32px;display:flex;gap:40px;">
  <div style="flex:1;">
    <div style="border-top:1px solid #000;width:200px;margin-bottom:4px;"></div>
    Ort, Datum, Unterschrift Antragsteller
  </div>
  <div style="flex:1;">
    <div style="border-top:1px solid #000;width:200px;margin-bottom:4px;"></div>
    Genehmigt / Abgelehnt / Mit Auflagen (Beh\u00f6rde)
  </div>
</div>

<p class="footer">
  Automatisch generierter Entwurf \u2022 FTTH Permit Engine \u2022
  Rechtlich unverbindlich \u2022 Vor Einreichung von einem Fachingenieur pru00fcfen lassen
</p>
</body></html>"""
        gslug = street.lower().replace(' ', '_')[:40] or 'unnamed'
        forms.append(
            {
                'name': f'german_aufbruch_{gslug}',
                'kind': 'FORM',
                'filename': f'forms/aufbruchgenehmigung_{gslug}.html',
                'content': content,
                'description': (
                    f'Aufbruchgenehmigung \u2013 {street} '
                    f'({len(members)} Abschnitte, {total_m:.0f} m)'
                ),
            }
        )
    return forms
