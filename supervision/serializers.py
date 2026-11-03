from rest_framework import serializers

from supervision.models import FeatureDecisionRecord, FeaturePhoto


class FeaturePhotoSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()
    uploaded_by = serializers.SerializerMethodField()

    class Meta:
        model = FeaturePhoto
        fields = [
            'id',
            'url',
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
        if not obj.image:
            return None
        request = self.context.get('request')
        if request:
            return request.build_absolute_uri(obj.image.url)
        return obj.image.url

    def get_uploaded_by(self, obj):
        if not obj.uploaded_by_id:
            return None
        user = obj.uploaded_by
        return user.full_name or user.email


class FeatureDecisionRecordSerializer(serializers.ModelSerializer):
    """One append-only decision record, flattened for the audit and lineage views."""

    feature_id = serializers.UUIDField(read_only=True)
    layer_id = serializers.CharField(source='feature.layer_id', read_only=True)
    layer_name = serializers.CharField(source='feature.layer_name', read_only=True)
    reviewer = serializers.SerializerMethodField()

    class Meta:
        model = FeatureDecisionRecord
        fields = [
            'id',
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
