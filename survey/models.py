"""Survey app models — FTTH field survey data.

Covers: GPS traces, trench classifications, CRM risk assessments,
hazards, field evidence, and survey change version control.
"""

import uuid
from django.db import models
from django.utils import timezone


# ── GPS Trace ──────────────────────────────────────────────────────────────

class GPSTrace(models.Model):
    """A recorded GPS trace during field survey."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    engineer = models.ForeignKey(
        'users.User',
        on_delete=models.CASCADE,
        related_name='gps_traces',
    )
    project = models.ForeignKey(
        'projects.Project',
        on_delete=models.CASCADE,
        related_name='gps_traces',
        null=True,
        blank=True,
    )
    started_at = models.DateTimeField(default=timezone.now)
    ended_at = models.DateTimeField(null=True, blank=True)
    total_distance_m = models.FloatField(null=True, blank=True, help_text='Total distance in meters')
    point_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['-started_at']
        verbose_name = 'GPS Trace'
        verbose_name_plural = 'GPS Traces'

    def __str__(self):
        return f"GPS Trace {self.id} — {self.started_at.strftime('%Y-%m-%d %H:%M')}"


class GPSPoint(models.Model):
    """Individual GPS point within a trace."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    trace = models.ForeignKey(
        GPSTrace,
        on_delete=models.CASCADE,
        related_name='points',
    )
    latitude = models.FloatField()
    longitude = models.FloatField()
    altitude = models.FloatField(null=True, blank=True)
    accuracy = models.FloatField(null=True, blank=True, help_text='GPS accuracy in meters')
    timestamp = models.DateTimeField(default=timezone.now)
    order = models.PositiveIntegerField(default=0, help_text='Order in the trace')

    class Meta:
        ordering = ['trace', 'order']
        verbose_name = 'GPS Point'
        verbose_name_plural = 'GPS Points'

    def __str__(self):
        return f"Point {self.order} @ ({self.latitude:.6f}, {self.longitude:.6f})"


# ── Trench Classifications ─────────────────────────────────────────────────

class TrenchSurvey(models.Model):
    """Engineering classification of a trench during survey."""
    class TrenchType(models.TextChoices):
        NEW_TRENCH = 'new_trench', 'New Trench'
        EXISTING_DUCT = 'existing_duct', 'Existing Duct'
        EXISTING_TRENCH = 'existing_trench', 'Existing Trench'
        EXISTING_FIBRE_ROUTE = 'existing_fibre_route', 'Existing Fibre Route'
        EXISTING_OPENREACH_DUCT = 'existing_openreach_duct', 'Existing Openreach Duct'
        EXISTING_VIRGIN_DUCT = 'existing_virgin_duct', 'Existing Virgin Duct'
        HDD_BORE = 'hdd_bore', 'HDD Bore'
        MOLE_PLOUGH = 'mole_plough', 'Mole Plough'
        MICRO_TRENCH = 'micro_trench', 'Micro Trench'
        SURFACE_MOUNTED = 'surface_mounted', 'Surface Mounted'
        POLE_ROUTE = 'pole_route', 'Pole Route'

    class ConstructionMethod(models.TextChoices):
        OPEN_CUT = 'open_cut', 'Open Cut'
        TRENCHLESS = 'trenchless', 'Trenchless'
        HDD = 'hdd', 'Horizontal Directional Drilling'
        MOLE = 'mole', 'Mole Plough'
        MICRO = 'micro', 'Micro Trenching'
        AERIAL = 'aerial', 'Aerial / Pole'

    class SurfaceType(models.TextChoices):
        ASPHALT = 'asphalt', 'Asphalt'
        CONCRETE = 'concrete', 'Concrete'
        PAVING = 'paving', 'Paving'
        EARTH = 'earth', 'Earth'
        GRASS = 'grass', 'Grass'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    engineer = models.ForeignKey(
        'users.User',
        on_delete=models.CASCADE,
        related_name='trench_surveys',
    )
    feature = models.ForeignKey(
        'projects.Feature',
        on_delete=models.CASCADE,
        related_name='trench_surveys',
    )
    trench_type = models.CharField(max_length=50, choices=TrenchType.choices, default=TrenchType.NEW_TRENCH)
    construction_method = models.CharField(max_length=30, choices=ConstructionMethod.choices, null=True, blank=True)
    depth_mm = models.PositiveIntegerField(null=True, blank=True, help_text='Depth in millimeters')
    width_mm = models.PositiveIntegerField(null=True, blank=True, help_text='Width in millimeters')
    surface_type = models.CharField(max_length=30, choices=SurfaceType.choices, null=True, blank=True)
    road_crossing = models.BooleanField(default=False)
    footpath_crossing = models.BooleanField(default=False)
    rail_crossing = models.BooleanField(default=False)
    river_crossing = models.BooleanField(default=False)
    private_property = models.BooleanField(default=False)
    traffic_sensitive = models.BooleanField(default=False)
    permit_required = models.BooleanField(default=False)
    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Trench Survey'
        verbose_name_plural = 'Trench Surveys'

    def __str__(self):
        return f"Trench {self.id} — {self.get_trench_type_display()}"


class ExistingAsset(models.Model):
    """Validation of existing infrastructure found during survey."""
    class AssetType(models.TextChoices):
        DUCT = 'duct', 'Existing Duct'
        CHAMBER = 'chamber', 'Existing Chamber'
        POLE = 'pole', 'Existing Pole'
        FIBRE = 'fibre', 'Existing Fibre'
        CABINET = 'cabinet', 'Existing Cabinet'
        HANDHOLE = 'handhole', 'Existing Handhole'
        JOINT = 'joint', 'Existing Joint'

    class Condition(models.TextChoices):
        REUSE_POSSIBLE = 'reuse_possible', 'Reuse Possible'
        NEEDS_REPAIR = 'needs_repair', 'Needs Repair'
        BLOCKED = 'blocked', 'Blocked'
        COLLAPSED = 'collapsed', 'Collapsed'
        FLOODED = 'flooded', 'Flooded'
        OCCUPIED = 'occupied', 'Occupied'
        UNKNOWN = 'unknown', 'Unknown'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    engineer = models.ForeignKey(
        'users.User',
        on_delete=models.CASCADE,
        related_name='existing_assets',
    )
    feature = models.ForeignKey(
        'projects.Feature',
        on_delete=models.CASCADE,
        related_name='existing_assets',
        null=True,
        blank=True,
    )
    asset_type = models.CharField(max_length=20, choices=AssetType.choices)
    condition = models.CharField(max_length=20, choices=Condition.choices, default=Condition.UNKNOWN)
    latitude = models.FloatField()
    longitude = models.FloatField()
    description = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Existing Asset'
        verbose_name_plural = 'Existing Assets'

    def __str__(self):
        return f"{self.get_asset_type_display()} — {self.get_condition_display()}"


# ── CRM / Risk Assessment ──────────────────────────────────────────────────

class RiskAssessment(models.Model):
    """Risk assessment attached to any survey asset."""
    class RiskCategory(models.TextChoices):
        TRAFFIC = 'traffic', 'Traffic'
        PEDESTRIAN = 'pedestrian', 'Pedestrian'
        PRIVATE_LAND = 'private_land', 'Private Land'
        TREE_ROOTS = 'tree_roots', 'Tree Roots'
        CONCRETE_SURFACE = 'concrete_surface', 'Concrete Surface'
        RAILWAY = 'railway', 'Railway'
        BRIDGE = 'bridge', 'Bridge'
        RIVER = 'river', 'River'
        PROTECTED_AREA = 'protected_area', 'Protected Area'
        ENVIRONMENTAL = 'environmental', 'Environmental'
        GAS_LINE = 'gas_line', 'Gas Line'
        WATER_MAIN = 'water_main', 'Water Main'
        ELECTRIC_CABLE = 'electric_cable', 'Electric Cable'
        TELECOM = 'telecom', 'Telecom'
        ASBESTOS = 'asbestos', 'Asbestos'
        CONFINED_SPACE = 'confined_space', 'Confined Space'

    class Severity(models.TextChoices):
        LOW = 'low', 'Low'
        MEDIUM = 'medium', 'Medium'
        HIGH = 'high', 'High'
        CRITICAL = 'critical', 'Critical'

    class Probability(models.TextChoices):
        RARE = 'rare', 'Rare'
        POSSIBLE = 'possible', 'Possible'
        LIKELY = 'likely', 'Likely'
        CERTAIN = 'certain', 'Certain'

    class Status(models.TextChoices):
        OPEN = 'open', 'Open'
        CLOSED = 'closed', 'Closed'
        ACCEPTED = 'accepted', 'Accepted'
        ESCALATED = 'escalated', 'Escalated'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    engineer = models.ForeignKey(
        'users.User',
        on_delete=models.CASCADE,
        related_name='risk_assessments',
    )
    feature = models.ForeignKey(
        'projects.Feature',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='risk_assessments',
    )
    trench_survey = models.ForeignKey(
        TrenchSurvey,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='risk_assessments',
    )
    category = models.CharField(max_length=30, choices=RiskCategory.choices)
    severity = models.CharField(max_length=10, choices=Severity.choices, default=Severity.MEDIUM)
    probability = models.CharField(max_length=10, choices=Probability.choices, default=Probability.POSSIBLE)
    mitigation = models.TextField(blank=True, default='')
    notes = models.TextField(blank=True, default='')
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.OPEN)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Risk Assessment'
        verbose_name_plural = 'Risk Assessments'

    def __str__(self):
        return f"Risk: {self.get_category_display()} ({self.get_severity_display()})"


class Hazard(models.Model):
    """Hazards identified during survey."""
    class HazardType(models.TextChoices):
        WORKING_AT_HEIGHT = 'working_at_height', 'Working at Height'
        CONFINED_SPACE = 'confined_space', 'Confined Space'
        EXCAVATION = 'excavation', 'Excavation'
        TRAFFIC_MANAGEMENT = 'traffic_management', 'Traffic Management'
        HIGH_VOLTAGE = 'high_voltage', 'High Voltage'
        FLOOD_RISK = 'flood_risk', 'Flood Risk'
        DOG = 'dog', 'Dog'
        AGGRESSIVE_RESIDENT = 'aggressive_resident', 'Aggressive Resident'
        PRIVATE_SECURITY = 'private_security', 'Private Security'
        ENVIRONMENTAL = 'environmental', 'Environmental Protection'
        TREE_ORDER = 'tree_order', 'Tree Preservation Order'

    class MitigationTemplate(models.TextChoices):
        TRAFFIC_LIGHTS = 'traffic_lights', 'Traffic Lights'
        TEMP_BARRIERS = 'temp_barriers', 'Temporary Barriers'
        ROAD_CLOSURE = 'road_closure', 'Road Closure'
        PERMIT = 'permit', 'Permit Required'
        HDD_ALT = 'hdd', 'Use HDD Alternative'
        NIGHT_WORK = 'night_work', 'Night Work'
        POLICE_ASSIST = 'police_assist', 'Police Assistance'
        TREE_OFFICER = 'tree_officer', 'Tree Officer Approval'
        ENV_APPROVAL = 'env_approval', 'Environmental Approval'
        UTILITY_LOCATE = 'utility_locate', 'Utility Locate'
        CAT_SCAN = 'cat_scan', 'CAT Scan'
        TRIAL_HOLE = 'trial_hole', 'Trial Hole'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    engineer = models.ForeignKey(
        'users.User',
        on_delete=models.CASCADE,
        related_name='hazards',
    )
    feature = models.ForeignKey(
        'projects.Feature',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='hazards',
    )
    hazard_type = models.CharField(max_length=30, choices=HazardType.choices)
    mitigation_template = models.CharField(max_length=20, choices=MitigationTemplate.choices, null=True, blank=True)
    notes = models.TextField(blank=True, default='')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Hazard'
        verbose_name_plural = 'Hazards'

    def __str__(self):
        return f"Hazard: {self.get_hazard_type_display()}"


# ── Field Evidence ─────────────────────────────────────────────────────────

class FieldEvidence(models.Model):
    """Photos, videos, voice notes, and measurements captured in the field."""
    class EvidenceType(models.TextChoices):
        PHOTO = 'photo', 'Photo'
        VIDEO = 'video', 'Video'
        VOICE_NOTE = 'voice_note', 'Voice Note'
        MEASUREMENT = 'measurement', 'Measurement'
        DOCUMENT = 'document', 'Document'
        SKETCH = 'sketch', 'Sketch'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    engineer = models.ForeignKey(
        'users.User',
        on_delete=models.CASCADE,
        related_name='field_evidence',
    )
    feature = models.ForeignKey(
        'projects.Feature',
        on_delete=models.SET_NULL,
        related_name='field_evidence',
        null=True,
        blank=True,
    )
    evidence_type = models.CharField(max_length=20, choices=EvidenceType.choices, default=EvidenceType.PHOTO)
    file = models.FileField(upload_to='survey/evidence/', null=True, blank=True)
    description = models.TextField(blank=True, default='')
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    weather = models.CharField(max_length=50, blank=True, default='')
    captured_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-captured_at']
        verbose_name = 'Field Evidence'
        verbose_name_plural = 'Field Evidence'

    def __str__(self):
        return f"{self.get_evidence_type_display()} — {self.captured_at.strftime('%Y-%m-%d %H:%M')}"


# ── Survey Change / Version Control ────────────────────────────────────────

class SurveyChange(models.Model):
    """Audit trail of every edit made during survey."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    engineer = models.ForeignKey(
        'users.User',
        on_delete=models.CASCADE,
        related_name='survey_changes',
    )
    feature = models.ForeignKey(
        'projects.Feature',
        on_delete=models.CASCADE,
        related_name='survey_changes',
    )
    field_name = models.CharField(max_length=100)
    old_value = models.JSONField(null=True, blank=True)
    new_value = models.JSONField(null=True, blank=True)
    reason = models.TextField(blank=True, default='')
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Survey Change'
        verbose_name_plural = 'Survey Changes'

    def __str__(self):
        return f"Change: {self.field_name} — {self.created_at.strftime('%Y-%m-%d %H:%M')}"


# ── Survey Status Tracking ─────────────────────────────────────────────────

class SurveyStatus(models.Model):
    """Overall survey status for a feature."""
    class StatusChoice(models.TextChoices):
        NOT_STARTED = 'not_started', 'Not Started'
        VISITED = 'visited', 'Visited'
        VERIFIED = 'verified', 'Verified'
        MODIFIED = 'modified', 'Modified'
        NEEDS_REVIEW = 'needs_review', 'Needs Review'
        REJECTED = 'rejected', 'Rejected'
        APPROVED = 'approved', 'Approved'
        COMPLETED = 'completed', 'Completed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    engineer = models.ForeignKey(
        'users.User',
        on_delete=models.CASCADE,
        related_name='survey_statuses',
    )
    feature = models.OneToOneField(
        'projects.Feature',
        on_delete=models.CASCADE,
        related_name='survey_status',
    )
    status = models.CharField(max_length=20, choices=StatusChoice.choices, default=StatusChoice.NOT_STARTED)
    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        verbose_name = 'Survey Status'
        verbose_name_plural = 'Survey Statuses'

    def __str__(self):
        return f"Feature {self.feature_id} — {self.get_status_display()}"


# ── Offline Sync Queue ─────────────────────────────────────────────────────

class SyncQueueItem(models.Model):
    """Tracks items pending sync from offline mobile app."""
    class ItemType(models.TextChoices):
        FEATURE_UPDATE = 'feature_update', 'Feature Update'
        PHOTO_UPLOAD = 'photo_upload', 'Photo Upload'
        GPS_TRACE = 'gps_trace', 'GPS Trace'
        RISK_ASSESSMENT = 'risk_assessment', 'Risk Assessment'
        HAZARD = 'hazard', 'Hazard'
        TRENCH_CLASSIFICATION = 'trench_classification', 'Trench Classification'

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        IN_PROGRESS = 'in_progress', 'In Progress'
        SYNCED = 'synced', 'Synced'
        FAILED = 'failed', 'Failed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    engineer = models.ForeignKey(
        'users.User',
        on_delete=models.CASCADE,
        related_name='sync_queue',
    )
    item_type = models.CharField(max_length=30, choices=ItemType.choices)
    entity_id = models.CharField(max_length=100, help_text='ID of the entity on the client')
    payload = models.JSONField(default=dict, help_text='Full payload to sync')
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.PENDING)
    retry_count = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(default=timezone.now)
    synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Sync Queue Item'
        verbose_name_plural = 'Sync Queue Items'

    def __str__(self):
        return f"Sync: {self.get_item_type_display()} — {self.get_status_display()}"


# ── Survey Feature (HLD/Survey Separation) ────────────────────────────────

class SurveyFeature(models.Model):
    """A survey-engineer copy of an HLD feature.

    The HLD generated by the Planning Platform is the source of truth and
    MUST NEVER be modified by the survey engineer.  Instead, every edit
    creates or updates a *Survey Feature* that references the original HLD
    feature.  Think of it like Git:

        HLD    = Original branch
        Survey = Working branch
        Planner = Merge request

    When no original_hld_feature is set the survey feature is a brand-new
    feature created by the engineer (e.g. a new PDP added in the field).
    """

    class SurveyStatus(models.TextChoices):
        NEW = 'new', 'New'
        MODIFIED = 'modified', 'Modified'
        REMOVED = 'removed', 'Removed'
        PENDING_REVIEW = 'pending_review', 'Pending Review'
        REJECTED = 'rejected', 'Rejected'
        APPROVED = 'approved', 'Approved'
        COMPLETED = 'completed', 'Completed'

    class SyncState(models.TextChoices):
        PENDING = 'pending', 'Pending'
        SYNCED = 'synced', 'Synced'
        FAILED = 'failed', 'Failed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Link back to the original HLD feature (null for engineer-created points)
    original_hld_feature = models.ForeignKey(
        'projects.Feature',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='survey_features',
        help_text='Original HLD feature this survey feature was derived from',
    )

    project = models.ForeignKey(
        'projects.Project',
        on_delete=models.CASCADE,
        related_name='survey_features',
    )

    engineer = models.ForeignKey(
        'users.User',
        on_delete=models.CASCADE,
        related_name='survey_features',
    )

    # Layer identification (mirrors the HLD layer)
    layer_id = models.CharField(max_length=255, db_index=True)
    layer_name = models.CharField(max_length=255)

    # Frozen copies from the HLD feature — never change after creation
    original_geometry = models.JSONField(null=True, blank=True, help_text='Frozen geometry from the HLD feature')
    original_attributes = models.JSONField(null=True, blank=True, help_text='Frozen attributes from the HLD feature')

    # Engineer-edited geometry and attributes
    survey_geometry = models.JSONField(help_text='Engineer-edited geometry')
    survey_attributes = models.JSONField(default=dict, help_text='Engineer-edited attributes')

    # Lifecycle
    survey_status = models.CharField(
        max_length=20,
        choices=SurveyStatus.choices,
        default=SurveyStatus.NEW,
    )
    version_number = models.PositiveIntegerField(default=1)
    sync_status = models.CharField(
        max_length=10,
        choices=SyncState.choices,
        default=SyncState.PENDING,
    )
    change_reason = models.TextField(blank=True, default='', help_text='Why the engineer made this change')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        verbose_name = 'Survey Feature'
        verbose_name_plural = 'Survey Features'
        indexes = [
            models.Index(fields=['project', 'layer_id']),
            models.Index(fields=['original_hld_feature']),
            models.Index(fields=['survey_status']),
            models.Index(fields=['sync_status']),
        ]

    def __str__(self):
        hld = str(self.original_hld_feature_id) if self.original_hld_feature_id else 'new'
        return f"SurveyFeature {self.id} — HLD:{hld} — {self.get_survey_status_display()}"
