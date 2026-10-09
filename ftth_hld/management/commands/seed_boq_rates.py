"""
Seed the BOQ rate card (``BoqRate``) from the original BOQ.xlsx template.

The template's BOQ sheet has rows of the form::

    # | Description | Unit | Quantity | Material €/unit | Labour €/unit | Rent €/unit | ...

Section header rows (e.g. ``"2. CIVIL WORKS – TRENCHING"``) carry no code in
column A beyond the bare section number; line items carry codes like ``2.1``.
Header detection: a row whose first cell matches ``^\\d+\\.\\s` and whose
second cell is non-empty.

Usage::

    python manage.py seed_boq_rates --xlsx path/to/BOQ.xlsx [--dry-run]

Idempotent: existing ``item_code`` rows are updated, new ones created.
"""

import re

from django.core.management.base import BaseCommand, CommandError

try:
    import openpyxl
except Exception:  # pragma: no cover
    openpyxl = None  # type: ignore


class Command(BaseCommand):
    help = 'Seed BoqRate rate-card rows from the BOQ.xlsx template.'

    def add_arguments(self, parser):
        parser.add_argument('--xlsx', required=True, help='Path to the BOQ.xlsx template.')
        parser.add_argument(
            '--dry-run', action='store_true', help='Print what would change without writing.'
        )

    def handle(self, *args, **opts):
        if openpyxl is None:
            raise CommandError('openpyxl is not installed.')
        from ftth_hld.models import BoqRate

        wb = openpyxl.load_workbook(opts['xlsx'], data_only=True)
        if 'BOQ' not in wb.sheetnames:
            raise CommandError(f"No 'BOQ' sheet in {opts['xlsx']}.")

        ws = wb['BOQ']
        rows = list(ws.iter_rows(values_only=True))

        # Find the header row (contains 'Description' and 'Unit').
        header_idx = None
        for i, row in enumerate(rows[:20]):
            vals = [str(c).strip().lower() if c is not None else '' for c in row]
            if 'description' in vals and 'unit' in vals:
                header_idx = i
                break
        if header_idx is None:
            raise CommandError('Could not locate the BOQ header row.')

        # Column mapping by header text.
        header = [str(c).strip().lower() if c is not None else '' for c in rows[header_idx]]

        def col(*names):
            for j, h in enumerate(header):
                if any(n in h for n in names):
                    return j
            return None

        i_desc = col('description', 'bezeich', 'item')
        i_unit = col('unit', 'einheit')
        i_mat = col('material')
        i_lab = col('labour', 'labor')
        i_rent = col('rent')

        current_section = ''
        created, updated, skipped = 0, 0, 0

        for row in rows[header_idx + 1 :]:
            code = row[0]
            code_s = str(code).strip() if code is not None else ''
            desc = row[i_desc] if i_desc is not None else None
            desc_s = str(desc).strip() if desc is not None else ''

            if not code_s and not desc_s:
                continue

            # Section header: a bare "2." code, or the whole section text in
            # column A (e.g. "2. CIVIL WORKS – TRENCHING"). Line items always
            # carry a dotted code like "2.1".
            is_line_item = bool(re.match(r'^\d+\.\d+', code_s))
            is_section = bool(
                re.match(r'^\d+\.\s*[A-Za-z]', code_s) or code_s.rstrip('.').isdigit()
            )
            if is_section and not is_line_item:
                current_section = desc_s or code_s.split('.', 1)[1].strip()
                continue

            # Line item: code like "2.1"
            if not code_s or not desc_s:
                continue

            def _num(v):
                if v is None:
                    return 0.0
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return 0.0

            unit = (
                str(row[i_unit]).strip() if i_unit is not None and row[i_unit] is not None else ''
            )
            mat = _num(row[i_mat]) if i_mat is not None else 0.0
            lab = _num(row[i_lab]) if i_lab is not None else 0.0
            rent = _num(row[i_rent]) if i_rent is not None else 0.0

            if opts['dry_run']:
                self.stdout.write(
                    f'  {code_s:6} {desc_s[:60]:60} {unit:8} '
                    f'mat={mat:g} lab={lab:g} rent={rent:g}'
                )
                created += 1
                continue

            rate, was_created = BoqRate.objects.update_or_create(
                item_code=code_s,
                defaults={
                    'section': current_section,
                    'item_name': desc_s,
                    'unit': unit,
                    'material_rate': mat,
                    'labour_rate': lab,
                    'rent_rate': rent,
                    'active': True,
                },
            )
            if was_created:
                created += 1
            else:
                updated += 1

        # Catalogue extensions beyond the template: the design deploys 1:16
        # and 1:64 splitters (per-PDP SPL_16 / SPL_64 attributes), which the
        # template's 1:32 / 1:8 splitter items cannot represent. Appended at
        # the end of PLANT ELEMENTS so the template layout stays intact.
        for code, name in (
            ('6.11', '1:16 Splitter on site'),
            ('6.12', '1:64 Splitter on site'),
        ):
            if opts['dry_run']:
                self.stdout.write(f'  {code:6} {name:60} ea       (catalogue extension)')
                created += 1
                continue
            rate, was_created = BoqRate.objects.update_or_create(
                item_code=code,
                defaults={
                    'section': 'PLANT ELEMENTS',
                    'item_name': name,
                    'unit': 'ea',
                    'material_rate': 0.0,
                    'labour_rate': 0.0,
                    'rent_rate': 0.0,
                    'active': True,
                },
            )
            if was_created:
                created += 1
            else:
                updated += 1

        verb = 'would create' if opts['dry_run'] else 'created'
        self.stdout.write(
            self.style.SUCCESS(
                f'Done: {verb} {created} rate rows ({updated} updated, {skipped} skipped).'
            )
        )
