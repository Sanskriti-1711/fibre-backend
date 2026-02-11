from rest_framework import serializers

from assignments.models import AssignmentJob
from projects.models import Feature, Project
from users.models import User


class AssignmentJobSerializer(serializers.ModelSerializer):
    project = serializers.PrimaryKeyRelatedField(queryset=Project.objects.all())
    feature = serializers.PrimaryKeyRelatedField(
        queryset=Feature.objects.all(), required=False, allow_null=True
    )
    assignee = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())

    class Meta:
        model = AssignmentJob
        fields = [
            "id",
            "project",
            "layer_id",
            "layer_name",
            "feature",
            "assignee",
            "scope",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def validate(self, attrs):
        scope = attrs.get("scope") or self.instance.scope
        project = attrs.get("project") or self.instance.project
        layer_id = attrs.get("layer_id") or self.instance.layer_id
        layer_name = attrs.get("layer_name") or self.instance.layer_name
        feature = attrs.get("feature") or self.instance.feature

        if scope == AssignmentJob.SCOPE_PROJECT:
            attrs["layer_id"] = None
            attrs["feature"] = None
        elif scope == AssignmentJob.SCOPE_LAYER:
            if not layer_id:
                raise serializers.ValidationError("Layer ID is required for layer scope")
            if not layer_name:
                raise serializers.ValidationError("Layer name is required for layer scope")
            attrs["feature"] = None
        elif scope == AssignmentJob.SCOPE_FEATURE:
            if not feature:
                raise serializers.ValidationError("Feature is required for feature scope")
            attrs["project"] = feature.project
            attrs["layer_id"] = feature.layer_id
            attrs["layer_name"] = feature.layer_name
        else:
            raise serializers.ValidationError("Invalid scope")

        if project and feature and feature.project_id != project.id:
            raise serializers.ValidationError("Feature must belong to the project")

        return attrs


class AssignmentJobDetailSerializer(AssignmentJobSerializer):
    assignee = serializers.SerializerMethodField()

    def get_assignee(self, obj):
        return {
            "id": str(obj.assignee_id),
            "email": obj.assignee.email,
            "role": obj.assignee.role,
        }

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["project_name"] = instance.project.name
        if instance.layer_id:
            data["layer"] = {
                "id": instance.layer_id,
                "name": instance.layer_name,
            }
        if instance.feature_id:
            data["feature"] = {
                "id": str(instance.feature_id),
                "status": instance.feature.status,
            }
        return data
