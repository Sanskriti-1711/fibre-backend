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
            "completion_percentage",
            "created_at",
            "updated_at",
            "last_activity_at",
        ]


class FeatureSerializer(serializers.ModelSerializer):
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
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields
