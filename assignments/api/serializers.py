from rest_framework import serializers

from assignments.models import AssignmentJob
from projects.models import Feature, Project
from users.models import User


class AssignmentJobSerializer(serializers.ModelSerializer):
    project = serializers.PrimaryKeyRelatedField(queryset=Project.objects.all())
    feature = serializers.PrimaryKeyRelatedField(
        queryset=Feature.objects.all(), required=False, allow_null=True
    )
    # Typed as the base Field: the detail serializer refines this into an
    # embedded object via SerializerMethodField (a Field subclass), which a
    # narrower declaration would reject as an incompatible override.
    assignee: serializers.Field = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())

    class Meta:
        model = AssignmentJob
        fields = [
            'id',
            'project',
            'layer_id',
            'feature',
            'polygon',
            'polygon_id',
            'polygon_name',
            'covered_feature_ids',
            'assignee',
            'scope',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['created_at', 'updated_at', 'covered_feature_ids']

    def validate(self, attrs):
        instance = getattr(self, 'instance', None)
        scope = attrs.get('scope') or getattr(instance, 'scope', None)
        project = attrs.get('project') or getattr(instance, 'project', None)
        layer_id = attrs.get('layer_id') or getattr(instance, 'layer_id', None)
        feature = attrs.get('feature') or getattr(instance, 'feature', None)

        if not scope:
            msg = 'Scope is required'
            raise serializers.ValidationError(msg)

        if scope == AssignmentJob.SCOPE_PROJECT:
            attrs['layer_id'] = None
            attrs['feature'] = None
        elif scope == AssignmentJob.SCOPE_LAYER:
            if not layer_id:
                msg = 'Layer ID is required for layer scope'
                raise serializers.ValidationError(msg)
            attrs['feature'] = None
        elif scope == AssignmentJob.SCOPE_FEATURE:
            if not feature:
                msg = 'Feature is required for feature scope'
                raise serializers.ValidationError(msg)
            attrs['project'] = feature.project
            attrs['layer_id'] = feature.layer_id
        elif scope == AssignmentJob.SCOPE_POLYGON:
            polygon_id = attrs.get('polygon_id') or getattr(instance, 'polygon_id', None)
            if not polygon_id:
                msg = 'Polygon ID is required for polygon scope'
                raise serializers.ValidationError(msg)
            attrs['feature'] = None
            attrs['layer_id'] = None
            attrs['polygon_id'] = polygon_id
            polygon = attrs.get('polygon') or getattr(instance, 'polygon', None)
            if not isinstance(polygon, dict):
                from assignments.coverage import resolve_polygon_geometry

                polygon, name = resolve_polygon_geometry(project, polygon_id)
                if not polygon:
                    msg = (
                        'Polygon geometry is required, or polygon_id must name a '
                        'polygon feature in this project'
                    )
                    raise serializers.ValidationError(msg)
                attrs['polygon'] = polygon
                if name and not attrs.get('polygon_name'):
                    attrs['polygon_name'] = name
            if polygon.get('type') not in ('Polygon', 'MultiPolygon'):
                msg = 'Polygon must be a GeoJSON Polygon or MultiPolygon'
                raise serializers.ValidationError(msg)
        else:
            msg = 'Invalid scope'
            raise serializers.ValidationError(msg)

        if project and feature and feature.project_id != project.id:
            msg = 'Feature must belong to the project'
            raise serializers.ValidationError(msg)

        return attrs

    def create(self, validated_data):
        scope = validated_data.get('scope')
        project = validated_data.get('project')
        layer_id = validated_data.get('layer_id')
        feature = validated_data.get('feature')

        assignee = validated_data.get('assignee')
        # A project or a layer can be held by several engineers, so only the same
        # engineer's job is replaced. Deleting the whole target would take the
        # other engineers' claims with it -- which is what made assigning a layer
        # to a second engineer look like a reassignment.
        if scope == AssignmentJob.SCOPE_PROJECT and project:
            AssignmentJob.objects.filter(
                project=project, scope=AssignmentJob.SCOPE_PROJECT, assignee=assignee
            ).delete()
        elif scope == AssignmentJob.SCOPE_LAYER and project and layer_id:
            AssignmentJob.objects.filter(
                project=project,
                scope=AssignmentJob.SCOPE_LAYER,
                layer_id=layer_id,
                assignee=assignee,
            ).delete()
        elif scope == AssignmentJob.SCOPE_FEATURE and feature:
            AssignmentJob.objects.filter(
                scope=AssignmentJob.SCOPE_FEATURE,
                feature=feature,
            ).delete()
        elif scope == AssignmentJob.SCOPE_POLYGON and project and validated_data.get('polygon_id'):
            AssignmentJob.objects.filter(
                project=project,
                scope=AssignmentJob.SCOPE_POLYGON,
                polygon_id=validated_data['polygon_id'],
                assignee=assignee,
            ).delete()

        return super().create(validated_data)


class AssignmentJobDetailSerializer(AssignmentJobSerializer):
    assignee = serializers.SerializerMethodField()

    def get_assignee(self, obj):
        return {
            'id': str(obj.assignee_id),
            'email': obj.assignee.email,
            'full_name': obj.assignee.full_name,
            'role': obj.assignee.role,
        }

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data['project_name'] = instance.project.name
        if instance.layer_id:
            data['layer'] = {
                'id': instance.layer_id,
            }
        if instance.feature_id:
            data['feature'] = {
                'id': str(instance.feature_id),
                'status': instance.feature.status,
            }
        if instance.scope == AssignmentJob.SCOPE_POLYGON:
            data['polygon'] = {
                'id': instance.polygon_id or '',
                'name': instance.polygon_name or instance.polygon_id or 'Assigned area',
                'geometry': instance.polygon,
                'feature_count': len(instance.covered_feature_ids or []),
            }
        return data
