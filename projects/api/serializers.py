from django.utils import timezone

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
            "source_ftth_project_id",
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
            "geometry",
            "field_schema",
            "field_measurements",
            "comparison_notes",
            "status",
            "photo_url",
            "edited_by",
            "edited_at",
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


class FeatureUpdateSerializer(serializers.ModelSerializer):
    """
    Writable serializer for PATCH — accepts partial updates to geometry and
    properties. Validates geometry is a valid GeoJSON geometry object.
    """

    class Meta:
        model = Feature
        fields = [
            "geometry",
            "properties",
            "field_measurements",
            "comparison_notes",
            "status",
            "edited_by",
        ]
        extra_kwargs = {
            "geometry": {"required": False},
            "properties": {"required": False},
            "field_measurements": {"required": False},
            "comparison_notes": {"required": False},
            "status": {"required": False},
            "edited_by": {"required": False},
        }

    def validate_geometry(self, value):
        if value is None:
            return value
        if not isinstance(value, dict):
            raise serializers.ValidationError("Geometry must be a JSON object")
        # Basic GeoJSON geometry validation
        geom_type = value.get("type")
        if geom_type not in (
            "Point", "MultiPoint",
            "LineString", "MultiLineString",
            "Polygon", "MultiPolygon",
            "GeometryCollection",
        ):
            raise serializers.ValidationError(
                f"Invalid GeoJSON geometry type: {geom_type}"
            )
        coordinates = value.get("coordinates")
        if not isinstance(coordinates, (list, tuple)):
            raise serializers.ValidationError(
                "Geometry must contain a coordinates array"
            )
        return value

    def update(self, instance, validated_data):
        # Auto-set the audit timestamp on any edit
        if any(k in validated_data for k in ("geometry", "properties", "field_measurements", "status")):
            validated_data["edited_at"] = timezone.now()
            if not validated_data.get("edited_by"):
                # Try to pull user from request context
                request = self.context.get("request")
                if request and request.user.is_authenticated:
                    validated_data["edited_by"] = request.user.id
        return super().update(instance, validated_data)
