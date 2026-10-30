import uuid

from django.conf import settings
from django.db import models

from projects.models import Feature


class FeaturePhoto(models.Model):
    """One of several site photos a field engineer attaches to a feature.

    Ported from the delivered supervision package
    (``Field_Supervision/fiber-backend-main/projects/models/feature_photo.py``).
    It lives in its own app because it is supervision-only: the design stages
    never attach evidence, they only produce the planned geometry.

    The old single ``Feature.photo`` column is kept (deprecated) until the UI
    stops reading it — see the integration plan G4. Ordering is ``order`` then
    ``uploaded_at`` so the first photo stays stable as the "primary" one.
    """

    MAX_PER_FEATURE = 20
    MAX_BYTES = 10 * 1024 * 1024  # 10 MB
    ALLOWED_CONTENT_TYPES = ['image/jpeg', 'image/jpg', 'image/png']

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    feature = models.ForeignKey(
        Feature,
        on_delete=models.CASCADE,
        related_name='photos',
    )
    image = models.ImageField(upload_to='feature_photos/%Y/%m/%d/')
    description = models.CharField(max_length=255, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='feature_photos',
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = 'feature_photos'
        ordering = ['order', 'uploaded_at']
        indexes = [
            models.Index(fields=['feature', 'order']),
        ]

    def __str__(self):
        return f'Photo {self.id} | feature {self.feature_id}'
