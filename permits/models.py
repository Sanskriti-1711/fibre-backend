"""Database models for the FTTH Permit system (Phase 1).

Implements the approved design in ``docs/subprojects/permit-engine/DESIGN.md``:

* permits are **identified** at HLD (authority mapping from route geometry),
  **evidenced** at Survey and **packaged** at LLD;
* permit determination is **deterministic GIS + rules** (never claimed from
  geometry alone) — each row records the rule that fired and its evidence;
* permits are **informational** in Phase 1: ``blocks_construction`` defaults to
  ``False`` and readiness % is a QA/reporting axis only, but the flag is in the
  schema so a future construction phase can enforce it per permit type;
* the authority registry is **multi-country from day one** (``country`` /
  ``region``), seeded with German/Berlin authorities.
"""

import uuid

from django.db import models


class PermitAuthority(models.Model):
    """Registry of authorities a permit can be submitted to.

    Multi-country from day one: ``country`` / ``region`` let the registry grow
    beyond Germany without a schema migration. Seeded with the German/Berlin
    authorities relevant to the current HLD data (Straßenbaulastträger by road
    class, Deutsche Bahn / regional rail, water authority, environmental
    agency, municipality/Gemeinde).
    """

    TYPE_MUNICIPALITY = "MUNICIPALITY"
    TYPE_ROAD = "ROAD"
    TYPE_RAIL = "RAIL"
    TYPE_WATER = "WATER"
    TYPE_ENVIRONMENTAL = "ENVIRONMENTAL"
    TYPE_PRIVATE = "PRIVATE"
    TYPE_TELECOM = "TELECOM"

    TYPE_CHOICES = [
        (TYPE_MUNICIPALITY, "Municipality / Gemeinde"),
        (TYPE_ROAD, "Road authority (Straßenbaulastträger)"),
        (TYPE_RAIL, "Railway operator (DB / regional rail)"),
        (TYPE_WATER, "Water authority"),
        (TYPE_ENVIRONMENTAL, "Environmental agency"),
        (TYPE_PRIVATE, "Private landowner"),
        (TYPE_TELECOM, "Telecom right-of-way"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=64, unique=True)  # e.g. DE-BE-STRASSENBAU
    name = models.CharField(max_length=255)
    authority_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    country = models.CharField(max_length=2, default="DE")
    region = models.CharField(max_length=64, blank=True, default="")  # e.g. BE
    contact = models.JSONField(default=dict)  # {email, phone, address, url}
    active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "ftth_permit_authorities"
        ordering = ["country", "region", "name"]

    def __str__(self):
        return f"{self.name} ({self.code})"


class PermitRule(models.Model):
    """Deterministic rule catalogue — the source of traceability.

    Every permit row records the rule that fired (``rule_id`` + a snapshot
    ``rule_version``) so the exact data/rule state can be reproduced. Rules
    only fire when the referenced layers/fields actually exist — a missing
    reference layer never implies \"no permit needed\"; it is recorded as a
    gap in the analysis notes.
    """

    LEVEL_REQUIRED = "REQUIRED"
    LEVEL_POTENTIAL = "POTENTIAL"

    LEVEL_CHOICES = [
        (LEVEL_REQUIRED, "Required"),
        (LEVEL_POTENTIAL, "Potentially required"),
    ]

    rule_id = models.CharField(max_length=64, unique=True)  # RAILWAY_CROSSING_001
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")

    # Layer pair the rule intersects, e.g. ("final_trenches", "osm_railway").
    layer_a = models.CharField(max_length=64)  # route layer
    layer_b = models.CharField(max_length=64, blank=True, default="")  # reference layer ('' = attribute rule)
    operator = models.CharField(max_length=32, default="INTERSECTS")  # INTERSECTS | WITHIN_BUFFER | ATTRIBUTE

    required_level = models.CharField(max_length=16, choices=LEVEL_CHOICES, default=LEVEL_POTENTIAL)
    authority = models.ForeignKey(
        "PermitAuthority",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="rules",
    )

    # Evidence keys the matrix must satisfy before a row becomes READY.
    evidence_required = models.JSONField(default=list)

    # Reserved for a future construction phase: when True and the platform
    # gains construction gating, APPROVED permits of this type block work.
    blocks_construction = models.BooleanField(default=False)

    version = models.IntegerField(default=1)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "ftth_permit_rules"
        ordering = ["rule_id"]

    def __str__(self):
        return f"{self.rule_id} v{self.version}"


class PermitMatrix(models.Model):
    """One permit per project per route section — the core Phase-1 object.

    Status flow:
      NOT_REQUIRED → IDENTIFIED → EVIDENCE_REQUIRED → READY → SUBMITTED
      → UNDER_REVIEW → APPROVED / REJECTED → CLOSED
    """

    STATUS_NOT_REQUIRED = "not_required"
    STATUS_IDENTIFIED = "identified"
    STATUS_EVIDENCE_REQUIRED = "evidence_required"
    STATUS_READY = "ready"
    STATUS_SUBMITTED = "submitted"
    STATUS_UNDER_REVIEW = "under_review"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_CLOSED = "closed"

    STATUS_CHOICES = [
        (STATUS_NOT_REQUIRED, "Not required"),
        (STATUS_IDENTIFIED, "Identified"),
        (STATUS_EVIDENCE_REQUIRED, "Evidence required"),
        (STATUS_READY, "Ready"),
        (STATUS_SUBMITTED, "Submitted"),
        (STATUS_UNDER_REVIEW, "Under review"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_CLOSED, "Closed"),
    ]

    permit_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        "ftth_hld.FtthProject",
        on_delete=models.CASCADE,
        related_name="permits",
    )
    route_section = models.CharField(max_length=128)  # trench/duct feature id
    layer = models.CharField(max_length=64)  # final_trenches | duct_layer | ...

    authority = models.ForeignKey(
        "PermitAuthority",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="permits",
    )
    permit_type = models.CharField(max_length=64)  # Road Opening | Railway Crossing | ...
    municipality = models.CharField(max_length=128, blank=True, default="")

    rule = models.ForeignKey(
        "PermitRule",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="permits",
    )
    rule_version = models.CharField(max_length=16, blank=True, default="")  # snapshot

    required = models.BooleanField(default=False)
    # Informational in Phase 1 — reserved for future construction gating.
    blocks_construction = models.BooleanField(default=False)

    status = models.CharField(
        max_length=24, choices=STATUS_CHOICES, default=STATUS_IDENTIFIED
    )
    readiness_pct = models.IntegerField(default=0)

    # Evidence attached so far: {evidence_key: {"present": bool, "refs": [...]}}.
    evidence = models.JSONField(default=dict)
    documents = models.JSONField(default=dict)
    analysis_notes = models.TextField(blank=True, default="")

    submission_date = models.DateTimeField(null=True, blank=True)
    approval_date = models.DateTimeField(null=True, blank=True)
    expiry_date = models.DateTimeField(null=True, blank=True)
    conditions = models.TextField(blank=True, default="")
    revision = models.IntegerField(default=0)
    comments = models.TextField(blank=True, default="")

    created_by = models.ForeignKey(
        "users.User", null=True, blank=True, on_delete=models.SET_NULL,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "ftth_permit_matrix"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["project", "status"]),
            models.Index(fields=["project", "route_section"]),
        ]

    def __str__(self):
        return f"{self.permit_type} {self.route_section} ({self.project_id})"


class PermitDocument(models.Model):
    """Generated permit-package files attached to a permit row."""

    KIND_CHOICES = [
        ("DRAWING", "Route drawing"),
        ("CROSS_SECTION", "Trench cross-section"),
        ("SCHEDULE", "Chamber / cabinet schedule"),
        ("TMP", "Traffic management plan"),
        ("FORM", "Permit application form"),
        ("REPORT", "Report"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    permit = models.ForeignKey(
        "PermitMatrix",
        on_delete=models.CASCADE,
        related_name="package_files",
    )
    name = models.CharField(max_length=255)
    kind = models.CharField(max_length=32, choices=KIND_CHOICES, default="REPORT")
    file = models.FileField(upload_to="permit_documents/%Y/%m/", null=True, blank=True)
    url = models.URLField(blank=True, default="")
    version = models.IntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "ftth_permit_documents"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} (v{self.version})"


class PermitEvent(models.Model):
    """Audit trail for a permit (identified → evidenced → submitted → …)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    permit = models.ForeignKey(
        "PermitMatrix",
        on_delete=models.CASCADE,
        related_name="events",
    )
    event = models.CharField(max_length=64)  # IDENTIFIED | EVIDENCE_ADDED | SUBMITTED | ...
    detail = models.JSONField(default=dict)
    actor = models.ForeignKey(
        "users.User", null=True, blank=True, on_delete=models.SET_NULL,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "ftth_permit_events"
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.event} ({self.permit_id})"
