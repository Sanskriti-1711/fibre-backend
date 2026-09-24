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

    # Statuses the readiness refresh auto-manages. Rows past this stage
    # (submitted → closed) belong to the review flow and are never
    # auto-flipped, even if their evidence later changes.
    AUTO_MANAGED_STATUSES = frozenset({
        STATUS_IDENTIFIED,
        STATUS_EVIDENCE_REQUIRED,
        STATUS_READY,
    })

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
    # Street-level grouping key (e.g. road name) for clubbing per-segment
    # rows into one permit per street. Segment rows stay for map colouring /
    # variation / traceability; the tracker and package forms aggregate by
    # this key. Blank = ungrouped (UTILITY_REUSE stays per asset).
    permit_group = models.CharField(max_length=128, blank=True, default="")

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

    # Phase-3: the submission this row was sent to the authority in. Null
    # while the row is pre-submission or after a variation re-opened it.
    submission = models.ForeignKey(
        "PermitSubmission",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="permit_rows",
    )

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

    def status_for_readiness(self, pct: int) -> str | None:
        """Status a row should hold for a readiness %, or None when the row
        is past the auto-managed stage (submitted and later).

        Implements the design's promotion rule: a row becomes READY as soon
        as its evidence checklist is satisfied, and drops back to
        EVIDENCE_REQUIRED (or IDENTIFIED) when evidence is missing again.
        Statuses SUBMITTED and later are owned by the review flow — the
        readiness refresh never touches them.
        """
        if self.status not in self.AUTO_MANAGED_STATUSES:
            return None
        if pct >= 100:
            return self.STATUS_READY
        if pct > 0:
            return self.STATUS_EVIDENCE_REQUIRED
        return self.STATUS_IDENTIFIED


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


class PermitSubmission(models.Model):
    """Phase-3 submission record — one application sent to an authority.

    Groups the street-level ``PermitMatrix`` rows that travel together in a
    single application (one per project × authority × permit type × street,
    matching the package's per-street application forms). The rows carry the
    per-segment detail and status mirror; the submission carries the
    application-level state: authority reference, review progress and the
    package version the application was built from.

    Status flow (Phase 3 design):
      SUBMITTED → UNDER_REVIEW → APPROVED / REJECTED → (CLOSED)
      REJECTED → SUBMITTED (re-submission as a new revision)

    ``sync_source`` records how a transition arrived — ``manual`` (planner
    flips it in the tracker) or a future portal/API poller
    (``portal``/``email``/``api``) — so the audit trail stays honest as
    status sync automates what manual entry did before.
    """

    STATUS_SUBMITTED = "submitted"
    STATUS_UNDER_REVIEW = "under_review"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_CLOSED = "closed"

    STATUS_CHOICES = [
        (STATUS_SUBMITTED, "Submitted"),
        (STATUS_UNDER_REVIEW, "Under review"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_CLOSED, "Closed"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        "ftth_hld.FtthProject",
        on_delete=models.CASCADE,
        related_name="permit_submissions",
    )
    authority = models.ForeignKey(
        "PermitAuthority",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="submissions",
    )
    permit_type = models.CharField(max_length=64)  # Road Opening | Railway Crossing | ...
    permit_group = models.CharField(max_length=128, blank=True, default="")
    label = models.CharField(max_length=255, blank=True, default="")

    status = models.CharField(
        max_length=24, choices=STATUS_CHOICES, default=STATUS_SUBMITTED
    )
    submission_date = models.DateTimeField(null=True, blank=True)
    reference = models.CharField(max_length=255, blank=True, default="")  # authority application no.
    notes = models.TextField(blank=True, default="")
    conditions = models.TextField(blank=True, default="")
    approval_date = models.DateTimeField(null=True, blank=True)
    expiry_date = models.DateTimeField(null=True, blank=True)
    revision = models.IntegerField(default=0)  # re-submissions of the same application
    package_version = models.IntegerField(null=True, blank=True)
    sync_source = models.CharField(max_length=32, default="manual")  # manual | portal | email | api

    created_by = models.ForeignKey(
        "users.User", null=True, blank=True, on_delete=models.SET_NULL,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "ftth_permit_submissions"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["project", "status"]),
            models.Index(fields=["authority", "status"]),
        ]

    def __str__(self):
        return f"{self.permit_type} {self.permit_group or '—'} ({self.status})"


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


class PermitAiDraft(models.Model):
    """AI-generated advisory draft — advisory only, never authoritative.

    Stores the text the copilot produced (cover paragraph, narrative,
    extracted checklist, completeness explanation, risk note, timeline note)
    so a reviewer can see what was suggested and what the deterministic
    fallback was. Nothing here ever writes to ``PermitMatrix.status`` /
    ``readiness_pct`` / ``required`` / ``blocks_construction`` — those are
    owned by the deterministic rule engine.
    """

    DRAFT_COVER = "cover"
    DRAFT_NARRATIVE = "narrative"
    DRAFT_REQUIREMENTS = "requirements_extract"
    DRAFT_COMPLETENESS = "completeness"
    DRAFT_RISK = "risk"
    DRAFT_TIMELINE = "timeline"

    DRAFT_CHOICES = [
        (DRAFT_COVER, "Cover text"),
        (DRAFT_NARRATIVE, "Narrative"),
        (DRAFT_REQUIREMENTS, "Requirements extract"),
        (DRAFT_COMPLETENESS, "Completeness explainer"),
        (DRAFT_RISK, "Risk note"),
        (DRAFT_TIMELINE, "Timeline note"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        "ftth_hld.FtthProject",
        on_delete=models.CASCADE,
        related_name="permit_ai_drafts",
    )
    permit = models.ForeignKey(
        "PermitMatrix",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ai_drafts",
    )
    permit_group = models.CharField(max_length=128, blank=True, default="")
    permit_type = models.CharField(max_length=64, blank=True, default="")
    draft_type = models.CharField(max_length=32, choices=DRAFT_CHOICES)
    content = models.TextField(blank=True, default="")
    deterministic_fallback = models.TextField(blank=True, default="")
    is_ai_generated = models.BooleanField(default=False)
    disclaimer = models.TextField(blank=True, default="")
    meta = models.JSONField(default=dict)  # model, prompt summary, extracted items
    created_by = models.ForeignKey(
        "users.User", null=True, blank=True, on_delete=models.SET_NULL,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed = models.BooleanField(default=False)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        "users.User", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="reviewed_permit_drafts",
    )

    class Meta:
        db_table = "ftth_permit_ai_drafts"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["project", "draft_type"]),
            models.Index(fields=["permit", "draft_type"]),
        ]

    def __str__(self):
        return f"{self.draft_type} {self.permit_type or '—'} ({self.project_id})"
