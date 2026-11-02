import uuid

from django.db import models

from .project import Project


class LayerFieldConfig(models.Model):
    """
    Admin-configured field schema for a layer, overriding the schema that was
    auto-derived at import time. Keyed by (project, layer_id) which is stable
    across re-imports, so admin edits survive a re-upload. When present, this
    schema is served to the engineer clients in place of the derived one.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE)

    layer_id = models.CharField(max_length=255)
    layer_name = models.CharField(max_length=255, blank=True)

    schema = models.JSONField(default=list)

    # Absolute slack, in the layer's own unit, that still counts as "as
    # designed". Read by the planned-vs-as-built comparison (§3 Stage 5.2);
    # 0 means any difference is a deviation.
    tolerance = models.DecimalField(max_digits=10, decimal_places=3, default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'layer_field_configs'
        unique_together = ('project', 'layer_id')

    def __str__(self):
        return f'{self.layer_name or self.layer_id} field config'
