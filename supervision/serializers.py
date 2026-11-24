from rest_framework import serializers

from config.media import media_url
from supervision import evidence_stamp
from supervision.models import FeatureDecisionRecord, FeaturePhoto


class FeaturePhotoSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()
    original_url = serializers.SerializerMethodField()
    uploaded_by = serializers.SerializerMethodField()
    project_id = serializers.UUIDField(read_only=True)
    project_name = serializers.CharField(source='project.name', read_only=True)

    class Meta:
        model = FeaturePhoto
        fields = [
            'id',
            'project_id',
            'project_name',
            'url',
            'original_url',
            'description',
            'uploaded_by',
            'uploaded_at',
            'order',
            # Provenance: what was uploaded, how big it was and where it went.
            'sha256',
            'size_bytes',
            'storage_backend',
        ]
        read_only_fields = fields

    def get_url(self, obj):
        """The stamped copy when there is one: the panel is what should be seen.

        The gallery, the review page and the app all read ``url``, so handing
        them the panel is what puts the reading and the place on the evidence
        without a second UI. ``original_url`` still serves the uploaded bytes --
        the ones ``sha256`` describes -- for anyone checking the file itself.
        """
        stamped = evidence_stamp.annotated_url(obj) if obj.annotated_image else ''
        if stamped:
            request = self.context.get('request')
            return request.build_absolute_uri(stamped) if request else stamped
        return self.get_original_url(obj)

    def get_original_url(self, obj):
        if not obj.image:
            return None
        url = media_url(obj.image)
        request = self.context.get('request')
        return request.build_absolute_uri(url) if request else url

    def get_uploaded_by(self, obj):
        if not obj.uploaded_by_id:
            return None
        from users.models import User

        try:
            user = User.objects.get(pk=obj.uploaded_by_id)
        except User.DoesNotExist:
            # A photo copied from the production corpus can name an uploader
            # this database does not have; the gallery should still render.
            return None
        return user.full_name or user.email


class FeatureDecisionRecordSerializer(serializers.ModelSerializer):
    """One append-only decision record, flattened for the audit and lineage views."""

    feature_id = serializers.UUIDField(read_only=True)
    project_id = serializers.UUIDField(read_only=True)
    project_name = serializers.CharField(source='project.name', read_only=True)
    layer_id = serializers.CharField(source='feature.layer_id', read_only=True)
    layer_name = serializers.CharField(source='feature.layer_name', read_only=True)
    reviewer = serializers.SerializerMethodField()

    class Meta:
        model = FeatureDecisionRecord
        fields = [
            'id',
            'project_id',
            'project_name',
            'feature_id',
            'layer_id',
            'layer_name',
            'stage',
            'decision',
            'reason',
            'deviation',
            'reviewer',
            'created_at',
        ]
        read_only_fields = fields

    def get_reviewer(self, obj):
        if not obj.reviewer_id:
            return None
        return obj.reviewer.full_name or obj.reviewer.email
