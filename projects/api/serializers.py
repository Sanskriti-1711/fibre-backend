from rest_framework import serializers
from projects.models.project import Project

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
