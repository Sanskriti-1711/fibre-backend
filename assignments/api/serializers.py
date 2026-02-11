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
            "feature",
            "assignee",
            "scope",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def validate(self, attrs):
        instance = getattr(self, "instance", None)
        scope = attrs.get("scope") or getattr(instance, "scope", None)
        project = attrs.get("project") or getattr(instance, "project", None)
        layer_id = attrs.get("layer_id") or getattr(instance, "layer_id", None)
        feature = attrs.get("feature") or getattr(instance, "feature", None)

        if not scope:
            raise serializers.ValidationError("Scope is required")

        if scope == AssignmentJob.SCOPE_PROJECT:
            attrs["layer_id"] = None
            attrs["feature"] = None
        elif scope == AssignmentJob.SCOPE_LAYER:
            if not layer_id:
                raise serializers.ValidationError("Layer ID is required for layer scope")
            attrs["feature"] = None
        elif scope == AssignmentJob.SCOPE_FEATURE:
            if not feature:
                raise serializers.ValidationError("Feature is required for feature scope")
            attrs["project"] = feature.project
            attrs["layer_id"] = feature.layer_id
        else:
            raise serializers.ValidationError("Invalid scope")

        if project and feature and feature.project_id != project.id:
            raise serializers.ValidationError("Feature must belong to the project")

        return attrs

    def create(self, validated_data):
        scope = validated_data.get("scope")
        project = validated_data.get("project")
        layer_id = validated_data.get("layer_id")
        feature = validated_data.get("feature")

        if scope == AssignmentJob.SCOPE_PROJECT and project:
            AssignmentJob.objects.filter(
                project=project, scope=AssignmentJob.SCOPE_PROJECT
            ).delete()
        elif scope == AssignmentJob.SCOPE_LAYER and project and layer_id:
            AssignmentJob.objects.filter(
                project=project,
                scope=AssignmentJob.SCOPE_LAYER,
                layer_id=layer_id,
            ).delete()
        elif scope == AssignmentJob.SCOPE_FEATURE and feature:
            AssignmentJob.objects.filter(
                scope=AssignmentJob.SCOPE_FEATURE,
                feature=feature,
            ).delete()

        return super().create(validated_data)


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
            }
        if instance.feature_id:
            data["feature"] = {
                "id": str(instance.feature_id),
                "status": instance.feature.status,
            }
        return data
