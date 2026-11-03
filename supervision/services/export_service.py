"""Whole-project Excel export.

Builds a single .xlsx workbook for a project: a Summary sheet with project
metadata and per-layer counts, then one sheet per layer holding every feature
in that layer. The per-layer column layout mirrors the layer CSV export in the
admin Reports page (schema order first, then any extra keys), so the two
exports read the same way.

The workbook is written in openpyxl's write-only mode and each layer is
streamed out of the database with an iterator, so a project with a large
feature count is never held in memory all at once.
"""

from datetime import date, datetime
from decimal import Decimal
from io import BytesIO

from django.db.models import Count, Max, Q
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from projects.models import Feature, LayerFieldConfig
from projects.services.completion_service import CompletionService

# Audit columns appended after a layer's data columns, with their fixed widths.
TRAILING_COLUMNS = [
    ("Status", 14),
    ("Assigned To", 24),
    ("Photos", 8),
    ("Comparison Notes", 32),
    ("Approval Notes", 32),
    ("Submitted At", 20),
    ("Approved At", 20),
    ("Last Updated", 20),
    ("Feature ID", 38),
]

SUMMARY_HEADERS = [
    ("Layer", 34),
    ("Sheet", 26),
    ("Features", 10),
    ("Pending", 10),
    ("Assigned", 10),
    ("Under Review", 14),
    ("Approved", 10),
    ("Redo", 8),
    ("Weight %", 10),
    ("Progress %", 11),
    ("Last Feature Update", 22),
]

STATUS_LABELS = dict(Feature.STATUS_CHOICES)

# Excel rejects these characters in a sheet name and caps the name at 31 chars.
INVALID_SHEET_CHARS = set("[]:*?/\\'")
MAX_SHEET_NAME = 31

# Excel's per-cell character limit.
MAX_CELL_CHARS = 32767

# Widths are clamped so one long note can't push a column off the screen.
MIN_COLUMN_WIDTH = 12
MAX_COLUMN_WIDTH = 50

HEADER_FILL = PatternFill("solid", start_color="1F2937")
HEADER_FONT = Font(bold=True, color="FFFFFF")
HEADER_ALIGNMENT = Alignment(vertical="center")
TITLE_FONT = Font(bold=True, size=14)
LABEL_FONT = Font(bold=True)


# ---------------------------------------------------------------- value coercion

def flatten(value):
    """Unwrap a stored cell into something Excel can hold.

    Properties are stored as ``{"value": ...}`` and field measurements as
    ``{"value": ..., "unit": ...}``. A report wants the measurement with its
    unit, not the JSON wrapper.
    """
    if value is None or isinstance(value, bool | int | float | Decimal):
        return value

    if isinstance(value, datetime | date):
        return value

    if isinstance(value, list | tuple):
        parts = [flatten(item) for item in value]
        return "; ".join(str(p) for p in parts if p not in (None, ""))

    if isinstance(value, dict):
        if "value" in value:
            inner = flatten(value["value"])
            unit = value.get("unit")
            if unit and inner not in (None, ""):
                return f"{inner} {unit}"
            return inner
        return "; ".join(
            f"{k}: {flatten(v)}" for k, v in value.items() if v is not None
        )

    return value


def excel_value(value):
    """Final coercion before a value goes into a cell."""
    value = flatten(value)

    if isinstance(value, Decimal):
        return float(value)

    if isinstance(value, datetime):
        # Excel has no concept of timezones; store the local wall-clock time.
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        return value.replace(tzinfo=None)

    if isinstance(value, str) and len(value) > MAX_CELL_CHARS:
        return value[: MAX_CELL_CHARS - 1] + "…"

    return value


def _data_cell(worksheet, value):
    """A cell, forced to text when the value would otherwise read as a formula.

    Imported GIS values can start with ``=``/``+``/``-``/``@``; openpyxl writes
    those as formulas, which Excel then evaluates or flags as an error.
    """
    value = excel_value(value)
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        cell = WriteOnlyCell(worksheet, value=value)
        cell.data_type = "s"
        return cell
    return value


def _header_cell(worksheet, label):
    cell = WriteOnlyCell(worksheet, value=label)
    cell.font = HEADER_FONT
    cell.fill = HEADER_FILL
    cell.alignment = HEADER_ALIGNMENT
    return cell


def _set_widths(worksheet, widths):
    """Apply column widths.

    Write-only sheets serialise their column definitions with the first row,
    so this has to run before anything is appended to ``worksheet``.
    """
    for index, width in enumerate(widths, start=1):
        worksheet.column_dimensions[get_column_letter(index)].width = width


def _width_for(text_length, header_length):
    return max(
        MIN_COLUMN_WIDTH,
        min(MAX_COLUMN_WIDTH, max(text_length, header_length) + 2),
    )


# ------------------------------------------------------------------ sheet naming

def sheet_title(name, used_titles):
    """A valid, unique sheet name for a layer.

    ``used_titles`` holds the casefolded names already taken and is updated in
    place; Excel treats sheet names case-insensitively.
    """
    cleaned = "".join(
        "-" if ch in INVALID_SHEET_CHARS else ch for ch in str(name or "")
    ).strip()
    cleaned = cleaned or "Layer"
    title = cleaned[:MAX_SHEET_NAME].strip() or "Layer"

    if title.casefold() not in used_titles:
        used_titles.add(title.casefold())
        return title

    # Collisions come from long names truncated to the same 31 characters, or
    # from two layers genuinely sharing a name.
    for counter in range(2, 1000):
        suffix = f" ({counter})"
        candidate = (cleaned[: MAX_SHEET_NAME - len(suffix)].strip() or "Layer") + suffix
        if candidate.casefold() not in used_titles:
            used_titles.add(candidate.casefold())
            return candidate

    used_titles.add(title.casefold())
    return title


# ----------------------------------------------------------------- column layout

def _order_key(entry):
    order = entry.get("order")
    return order if isinstance(order, int | float) and not isinstance(order, bool) else 0


def _schema_columns(schema):
    """Schema entries as (key, label) pairs in their configured order."""
    if not isinstance(schema, list):
        return []

    columns = []
    seen = set()
    for entry in sorted(
        (e for e in schema if isinstance(e, dict) and e.get("key")), key=_order_key
    ):
        key = entry["key"]
        if key in seen:
            continue
        seen.add(key)
        columns.append((key, entry.get("label") or key))
    return columns


def build_columns(schema, key_rows):
    """Ordered (key, label, width) columns for a layer.

    ``key_rows`` is an iterable of ``(properties, field_measurements)`` pairs.
    Schema fields lead, in their configured order and with their human labels;
    any key the schema does not mention follows, so nothing recorded against a
    feature is dropped from the export. Widths are measured over the same pass
    because write-only sheets need them before the first row is written.
    """
    columns = _schema_columns(schema)
    order = [key for key, _ in columns]
    labels = dict(columns)
    seen = set(order)
    text_widths = {key: 0 for key in order}

    for properties, measurements in key_rows:
        for source in (properties, measurements):
            if not isinstance(source, dict):
                continue
            for key, value in source.items():
                if key not in seen:
                    seen.add(key)
                    order.append(key)
                    labels[key] = key
                    text_widths[key] = 0
                flat = flatten(value)
                if flat is not None:
                    text_widths[key] = max(text_widths[key], len(str(flat)))

    return [
        (key, labels[key], _width_for(text_widths.get(key, 0), len(labels[key])))
        for key in order
    ]


# ------------------------------------------------------------------- assignments

def _assignment_lookup(project):
    """Resolve who each feature is assigned to.

    Assignments are recorded at project, layer, or feature scope; the narrowest
    scope wins. Returns ``(project_assignee, by_layer, by_feature)``.
    """
    # Imported here rather than at module level: the assignments app imports
    # projects.models, so a top-level import would tie the two together.
    from assignments.models import AssignmentJob

    project_assignee = None
    by_layer = {}
    by_feature = {}

    for job in AssignmentJob.objects.filter(project=project).select_related("assignee"):
        assignee = job.assignee
        label = (assignee.full_name or assignee.email) if assignee else None
        if job.scope == AssignmentJob.SCOPE_PROJECT:
            project_assignee = label
        elif job.scope == AssignmentJob.SCOPE_LAYER and job.layer_id:
            by_layer[job.layer_id] = label
        elif job.scope == AssignmentJob.SCOPE_FEATURE and job.feature_id:
            by_feature[str(job.feature_id)] = label

    return project_assignee, by_layer, by_feature


# --------------------------------------------------------------- layer metadata

def layer_summaries(project):
    """Per-layer counts, ordered the way the layers list endpoint orders them."""
    rows = (
        Feature.objects.filter(project=project)
        .values("layer_id", "layer_name")
        .annotate(
            feature_count=Count("id"),
            pending_count=Count("id", filter=Q(status=Feature.STATUS_PENDING)),
            assigned_count=Count("id", filter=Q(status=Feature.STATUS_ASSIGNED)),
            under_review_count=Count(
                "id", filter=Q(status=Feature.STATUS_UNDER_REVIEW)
            ),
            approved_count=Count("id", filter=Q(status=Feature.STATUS_APPROVED)),
            redo_count=Count("id", filter=Q(status=Feature.STATUS_REDO)),
            last_feature_update=Max("updated_at"),
        )
        .order_by("layer_name")
    )

    return [
        {
            "layer_id": row["layer_id"],
            "layer_name": row["layer_name"],
            "feature_count": row["feature_count"],
            "last_feature_update": row["last_feature_update"],
            "status_counts": {
                "pending": row["pending_count"],
                "assigned": row["assigned_count"],
                "under_review": row["under_review_count"],
                "approved": row["approved_count"],
                "redo": row["redo_count"],
            },
        }
        for row in rows
    ]


def _layer_schemas(project, layer_ids):
    """Effective field schema per layer: admin config when set, else derived.

    Matches what the feature detail endpoint serves the engineer clients.
    """
    schemas = {}

    for config in LayerFieldConfig.objects.filter(
        project=project, layer_id__in=layer_ids
    ):
        schemas[config.layer_id] = config.schema

    for layer_id in layer_ids:
        if layer_id in schemas:
            continue
        feature = (
            Feature.objects.filter(project=project, layer_id=layer_id)
            .exclude(field_schema__isnull=True)
            .only("field_schema")
            .first()
        )
        schemas[layer_id] = feature.field_schema if feature else None

    return schemas


# ----------------------------------------------------------------------- sheets

def _write_layer_sheet(workbook, title, project, layer_id, schema, assignments):
    """Write one layer's features to a new sheet. Returns the row count."""
    project_assignee, by_layer, by_feature = assignments

    base_qs = Feature.objects.filter(project=project, layer_id=layer_id)

    # First pass reads only the JSON columns, so the column union and widths
    # are known before the header row without loading whole feature rows.
    columns = build_columns(
        schema,
        base_qs.values_list("properties", "field_measurements").iterator(
            chunk_size=1000
        ),
    )

    headers = [label for _, label, _ in columns] + [
        label for label, _ in TRAILING_COLUMNS
    ]
    widths = [width for _, _, width in columns] + [
        width for _, width in TRAILING_COLUMNS
    ]

    worksheet = workbook.create_sheet(title)
    _set_widths(worksheet, widths)
    worksheet.freeze_panes = "A2"
    worksheet.append([_header_cell(worksheet, header) for header in headers])

    row_count = 0
    features = (
        base_qs.order_by("created_at").prefetch_related("photos").iterator(chunk_size=500)
    )

    for feature in features:
        properties = feature.properties if isinstance(feature.properties, dict) else {}
        measurements = (
            feature.field_measurements
            if isinstance(feature.field_measurements, dict)
            else {}
        )
        # Engineer-entered measurements override the imported reference values.
        merged = {**properties, **measurements}

        assignee = (
            by_feature.get(str(feature.id))
            or by_layer.get(feature.layer_id)
            or project_assignee
        )

        values = [merged.get(key) for key, _, _ in columns] + [
            STATUS_LABELS.get(feature.status, feature.status),
            assignee or "",
            len(feature.photos.all()),
            feature.comparison_notes or "",
            feature.approval_notes or "",
            feature.submitted_at,
            feature.approved_at,
            feature.updated_at,
            str(feature.id),
        ]

        worksheet.append([_data_cell(worksheet, value) for value in values])
        row_count += 1

    if row_count:
        worksheet.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{row_count + 1}"

    return row_count


def _write_summary_header(worksheet, project, completion, generated_by):
    title = WriteOnlyCell(worksheet, value=f"{project.name} — Project Export")
    title.font = TITLE_FONT
    worksheet.append([title])
    worksheet.append([])

    meta = [
        ("Project", project.name),
        ("Region", project.region or "—"),
        ("Status", project.get_status_display()),
        ("Description", project.description or "—"),
        ("Total Features", completion["total_features"]),
        ("Approved Features", completion["approved_features"]),
        ("Standard Completion %", completion["standard_completion"]),
        ("Dynamic Completion %", completion["dynamic_completion"]),
        ("Layer Weights Defined", "Yes" if completion["weights_defined"] else "No"),
        ("Generated At", timezone.localtime(timezone.now())),
        ("Generated By", generated_by or "—"),
    ]

    for label, value in meta:
        label_cell = WriteOnlyCell(worksheet, value=label)
        label_cell.font = LABEL_FONT
        worksheet.append([label_cell, _data_cell(worksheet, value)])

    worksheet.append([])


def _write_summary_table(worksheet, rows):
    worksheet.append([_header_cell(worksheet, label) for label, _ in SUMMARY_HEADERS])
    for row in rows:
        worksheet.append([_data_cell(worksheet, value) for value in row])


# -------------------------------------------------------------------- entrypoint

def build_project_workbook(project, generated_by=None):
    """Return a BytesIO holding the project's whole-project .xlsx export."""
    completion = CompletionService.calculate_project_completion(project)
    completion_by_layer = {layer["layer_id"]: layer for layer in completion["layers"]}

    summaries = layer_summaries(project)
    schemas = _layer_schemas(project, [s["layer_id"] for s in summaries])
    assignments = _assignment_lookup(project)

    workbook = Workbook(write_only=True)

    # Summary is created first so it lands as the leftmost tab, but its layer
    # table can only be written once each layer's sheet name is settled. Rows
    # for a write-only sheet can be appended at any point before save, so the
    # sheet is opened here and finished at the end.
    summary = workbook.create_sheet("Summary")
    _set_widths(summary, [width for _, width in SUMMARY_HEADERS])
    _write_summary_header(summary, project, completion, generated_by)

    used_titles = {"summary"}
    summary_rows = []

    for entry in summaries:
        layer_id = entry["layer_id"]
        title = sheet_title(entry["layer_name"] or layer_id, used_titles)

        _write_layer_sheet(
            workbook, title, project, layer_id, schemas.get(layer_id), assignments
        )

        stats = completion_by_layer.get(layer_id, {})
        counts = entry["status_counts"]
        summary_rows.append(
            [
                entry["layer_name"] or layer_id,
                title,
                entry["feature_count"],
                counts["pending"],
                counts["assigned"],
                counts["under_review"],
                counts["approved"],
                counts["redo"],
                stats.get("weight"),
                stats.get("progress_percentage"),
                entry["last_feature_update"],
            ]
        )

    _write_summary_table(summary, summary_rows)

    if not summaries:
        # A project with no imported features still gets a readable workbook.
        empty = workbook.create_sheet("No Layers")
        empty.append(["This project has no imported features yet."])

    stream = BytesIO()
    workbook.save(stream)
    stream.seek(0)
    return stream


def export_filename(project, now=None):
    """``Project-Name_2026-09-07.xlsx``, safe for a Content-Disposition header."""
    stamp = (now or timezone.localtime(timezone.now())).strftime("%Y-%m-%d")
    slug = "".join(
        ch if ch.isalnum() else "-" for ch in str(project.name or "project")
    )
    while "--" in slug:
        slug = slug.replace("--", "-")
    slug = slug.strip("-") or "project"
    return f"{slug[:80]}_{stamp}.xlsx"
