import uuid

from django.db import models

from .project import Project
from .feature import Feature


class SurveyChange(models.Model):
    """
    Represents a change made by a survey engineer to an HLD feature.
    
    Survey changes track modifications to the original HLD design,
    including geometry changes, attribute changes, new features,
    and removed features.
    """
    
    # Change types
    CHANGE_TYPE_GEOMETRY = "geometry"
    CHANGE_TYPE_ATTRIBUTE = "attribute"
    CHANGE_TYPE_NEW_FEATURE = "new_feature"
    CHANGE_TYPE_REMOVED_FEATURE = "removed_feature"
    
    CHANGE_TYPE_CHOICES = [
        (CHANGE_TYPE_GEOMETRY, "Geometry Change"),
        (CHANGE_TYPE_ATTRIBUTE, "Attribute Change"),
        (CHANGE_TYPE_NEW_FEATURE, "New Feature"),
        (CHANGE_TYPE_REMOVED_FEATURE, "Removed Feature"),
    ]
    
    # Review status
    STATUS_PENDING = "pending_review"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_NEEDS_CORRECTION = "needs_correction"
    
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending Review"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_NEEDS_CORRECTION, "Needs Correction"),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="survey_changes")
    
    # Reference to the original HLD feature (null for new features)
    original_feature = models.ForeignKey(
        Feature, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        related_name="survey_changes_as_original"
    )
    
    # For new features, store the proposed feature data
    proposed_feature = models.ForeignKey(
        Feature, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        related_name="survey_changes_as_proposed"
    )
    
    # Change metadata
    change_type = models.CharField(max_length=20, choices=CHANGE_TYPE_CHOICES)
    layer_name = models.CharField(max_length=255)
    feature_id_display = models.CharField(max_length=255, help_text="Display ID for the feature")
    
    # Original HLD values (for attribute changes)
    original_geometry = models.JSONField(null=True, blank=True, help_text="Original HLD geometry")
    original_attributes = models.JSONField(null=True, blank=True, help_text="Original HLD attributes")
    
    # Survey proposed values
    proposed_geometry = models.JSONField(null=True, blank=True, help_text="Proposed survey geometry")
    proposed_attributes = models.JSONField(null=True, blank=True, help_text="Proposed survey attributes")
    
    # For attribute changes, store the specific field that changed
    changed_field = models.CharField(max_length=255, blank=True, help_text="Name of changed attribute")
    original_value = models.JSONField(null=True, blank=True, help_text="Original field value")
    proposed_value = models.JSONField(null=True, blank=True, help_text="Proposed field value")
    
    # Reason and notes
    reason = models.TextField(help_text="Reason for the change")
    engineer_notes = models.TextField(blank=True, help_text="Additional notes from engineer")
    
    # Review information
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    reviewer = models.ForeignKey(
        'users.User', 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        related_name="reviewed_changes"
    )
    review_comments = models.TextField(blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    
    # Audit trail
    created_by = models.ForeignKey(
        'users.User', 
        on_delete=models.SET_NULL, 
        null=True, 
        related_name="created_changes"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = "survey_changes"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["project", "status"]),
            models.Index(fields=["change_type"]),
            models.Index(fields=["layer_name"]),
        ]
    
    def __str__(self):
        return f"{self.change_type} - {self.feature_id_display} ({self.get_status_display()})"
    
    @property
    def is_approved(self):
        return self.status == self.STATUS_APPROVED
    
    @property
    def is_rejected(self):
        return self.status == self.STATUS_REJECTED
    
    @property
    def is_pending(self):
        return self.status == self.STATUS_PENDING
    
    @property
    def needs_correction(self):
        return self.status == self.STATUS_NEEDS_CORRECTION
