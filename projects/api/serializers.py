from django.utils import timezone
from rest_framework import serializers

from config.media import media_url
from projects.models.feature import Feature
from projects.models.project import Project


class ProjectSerializer(serializers.ModelSerializer):
    # How this project came to exist: 'lld_run' (opened from LLD Outputs) or
    # 'manual_gpkg' (contractor upload). Read-only: the create view stamps it,
    # because the value is a consequence of the source_lld_run_id it validated,
    # not something a client should be able to assert. INTEGRATION_PLAN.md §3.3.
    origin = serializers.SerializerMethodField()

    class Meta:
        model = Project
        fields = [
            'id',
            'name',
            'description',
            'region',
            'status',
            'standard_completion',
            'source_ftth_project_id',
            'source_lld_run_id',
            'origin',
            'created_at',
            'updated_at',
            'last_activity_at',
        ]

    def get_origin(self, obj):
        return (obj.business_meta or {}).get('origin') or 'manual_gpkg'


GEOJSON_GEOMETRY_TYPES = (
    'Point',
    'MultiPoint',
    'LineString',
    'MultiLineString',
    'Polygon',
    'MultiPolygon',
    'GeometryCollection',
)


def validate_geojson_geometry(value):
    """Shared GeoJSON geometry check for the planned and as-built columns."""
    if value is None:
        return value
    if not isinstance(value, dict):
        msg = 'Geometry must be a JSON object'
        raise serializers.ValidationError(msg)
    geom_type = value.get('type')
    if geom_type not in GEOJSON_GEOMETRY_TYPES:
        msg = f'Invalid GeoJSON geometry type: {geom_type}'
        raise serializers.ValidationError(msg)
    coordinates = value.get('coordinates')
    if not isinstance(coordinates, list | tuple):
        msg = 'Geometry must contain a coordinates array'
        raise serializers.ValidationError(msg)
    return value


class FeatureSerializer(serializers.ModelSerializer):
    photo_url = serializers.SerializerMethodField()

    class Meta:
        model = Feature
        fields = [
            'id',
            'layer_name',
            'layer_id',
            'properties',
            'geometry',
            'as_built_geometry',
            'field_schema',
            'field_measurements',
            'comparison_notes',
            'approval_notes',
            'status',
            'photo_url',
            'edited_by',
            'edited_at',
            'created_at',
            'updated_at',
        ]
        read_only_fields = fields

    def get_photo_url(self, obj):
        if not obj.photo:
            return None
        url = media_url(obj.photo)
        request = self.context.get('request')
        return request.build_absolute_uri(url) if request else url


class FeatureUpdateSerializer(serializers.ModelSerializer):
    """
    Writable serializer for PATCH — accepts partial updates to geometry and
    properties. Validates geometry is a valid GeoJSON geometry object.
    """

    class Meta:
        model = Feature
        fields = [
            'geometry',
            'as_built_geometry',
            'properties',
            'field_measurements',
            'comparison_notes',
            'status',
            'edited_by',
        ]
        extra_kwargs = {
            'geometry': {'required': False},
            'as_built_geometry': {'required': False},
            'properties': {'required': False},
            'field_measurements': {'required': False},
            'comparison_notes': {'required': False},
            'status': {'required': False},
            'edited_by': {'required': False},
        }

    def validate_geometry(self, value):
        return validate_geojson_geometry(value)

    def validate_as_built_geometry(self, value):
        return validate_geojson_geometry(value)

    def update(self, instance, validated_data):
        # Auto-set the audit timestamp on any edit
        if any(
            k in validated_data
            for k in (
                'geometry',
                'as_built_geometry',
                'properties',
                'field_measurements',
                'status',
            )
        ):
            validated_data['edited_at'] = timezone.now()
            if not validated_data.get('edited_by'):
                # Try to pull user from request context
                request = self.context.get('request')
                if request and request.user.is_authenticated:
                    validated_data['edited_by'] = request.user.id
        return super().update(instance, validated_data)
