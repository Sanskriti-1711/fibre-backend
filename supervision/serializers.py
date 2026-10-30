from rest_framework import serializers

from supervision.models import FeaturePhoto


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
