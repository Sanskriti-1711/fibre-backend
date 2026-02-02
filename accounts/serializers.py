from rest_framework import serializers
from rest_framework.validators import UniqueValidator
from django.contrib.auth import authenticate
from .models import User


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)


class UserCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = [
            "password",
            "email",
            "phone",
            "role",
        ]
        extra_kwargs = {
            "password": {"write_only": True},
            "email": {
                "validators": [UniqueValidator(queryset=User.objects.all())]
            },
            "phone": {"required": False, "allow_blank": True},
        }

    def create(self, validated_data):
        email = validated_data.pop("email").lower()
        password = validated_data.pop("password")
        user = User.objects.create_user(
            username=email,
            email=email,
            password=password,
            **validated_data,
        )
        return user
