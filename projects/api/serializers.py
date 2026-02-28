from rest_framework import serializers

from projects.models.project import Project
from projects.models.feature import Feature


class ProjectSerializer(serializers.ModelSerializer):
    class Meta:
        model = Project
        fields = [
            "id",
            "name",
            "description",
            "region",
            "status",
            "standard_completion",
            "created_at",
            "updated_at",
            "last_activity_at",
        ]


class FeatureSerializer(serializers.ModelSerializer):
    photo_url = serializers.SerializerMethodField()

    class Meta:
        model = Feature
        fields = [
            "id",
            "layer_name",
            "layer_id",
            "properties",
            "field_measurements",
            "comparison_notes",
            "status",
            "photo_url",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_photo_url(self, obj):
        if obj.photo:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.photo.url)
            return obj.photo.url
        return None
