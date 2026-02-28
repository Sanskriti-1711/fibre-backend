from django.contrib.auth import authenticate
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from .models import User


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("id", "email", "full_name", "role", "created_by", "created_at")
        read_only_fields = fields


class UserCreateSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=True, trim_whitespace=False)

    class Meta:
        model = User
        fields = ("id", "email", "full_name", "password", "role")
        read_only_fields = ("id",)

    def validate_password(self, value):
        validate_password(value)
        return value

    def validate_role(self, value):
        if value not in (User.Role.SUBADMIN, User.Role.ENGINEER):
            raise serializers.ValidationError("Invalid role")
        return value

    def create(self, validated_data):
        request = self.context.get("request")
        created_by = None
        if request and getattr(request, "user", None) and request.user.is_authenticated:
            if validated_data.get("role") == User.Role.ENGINEER:
                created_by = request.user

        return User.objects.create_user(
            email=validated_data["email"],
            password=validated_data["password"],
            full_name=validated_data.get("full_name"),
            role=validated_data.get("role", User.Role.ENGINEER),
            created_by=created_by,
        )


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(trim_whitespace=False)

    def validate(self, attrs):
        email = attrs.get("email")
        password = attrs.get("password")

        user = authenticate(self.context.get("request"), username=email, password=password)
        if not user:
            raise serializers.ValidationError("Invalid credentials")
        if not user.is_active:
            raise serializers.ValidationError("User is inactive")

        attrs["user"] = user
        return attrs
