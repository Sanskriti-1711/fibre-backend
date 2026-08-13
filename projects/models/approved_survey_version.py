import uuid

from django.db import models

from .project import Project


class ApprovedSurveyVersion(models.Model):
    """
    Immutable snapshot of approved survey changes applied to HLD baseline.
    
    This represents the authoritative input to LLD generation.
    Once created, it should never be modified. Create a new version
    if another review cycle is required.
    
    Conceptually:
        Approved Survey = HLD + Approved Changes - Approved Removals
    """
    
    STATUS_DRAFT = "draft"
    STATUS_READY = "ready"
    STATUS_USED_FOR_LLD = "used_for_lld"
    
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_READY, "Ready for LLD"),
        (STATUS_USED_FOR_LLD, "Used for LLD"),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="approved_survey_versions")
    
    # Version tracking
    version_number = models.PositiveIntegerField(help_text="Sequential version number")
    
    # Source references
    hld_project_id = models.CharField(max_length=64, help_text="Source HLD project ID")
    
    # Statistics
    total_changes = models.PositiveIntegerField(default=0)
    approved_changes = models.PositiveIntegerField(default=0)
    rejected_changes = models.PositiveIntegerField(default=0)
    
    # The approved data snapshot (stored as JSON for immutability)
    # This contains the complete approved dataset that can be used as LLD input
    approved_data = models.JSONField(help_text="Complete approved survey dataset")
    
    # Metadata
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    
    # Audit trail
    created_by = models.ForeignKey(
        'users.User', 
        on_delete=models.SET_NULL, 
        null=True, 
        related_name="created_approved_versions"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    
    # LLD run tracking
    lld_run_id = models.CharField(max_length=64, blank=True, help_text="LLD run ID if used")
    lld_run_at = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        db_table = "approved_survey_versions"
        ordering = ["-version_number"]
        unique_together = ["project", "version_number"]
        indexes = [
            models.Index(fields=["project", "status"]),
            models.Index(fields=["version_number"]),
        ]
    
    def __str__(self):
        return f"Approved Survey V{self.version_number} - {self.project.name}"
    
    @property
    def is_ready_for_lld(self):
        return self.status == self.STATUS_READY
    
    @property
    def is_used(self):
        return self.status == self.STATUS_USED_FOR_LLD
    
    def mark_as_used_for_lld(self, lld_run_id):
        """Mark this version as used for an LLD run."""
        self.status = self.STATUS_USED_FOR_LLD
        self.lld_run_id = lld_run_id
        from django.utils import timezone
        self.lld_run_at = timezone.now()
        self.save()
